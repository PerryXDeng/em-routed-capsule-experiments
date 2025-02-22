"""
License: Apache 2.0
Author: Perry Deng
E-mail: perry.deng@mail.rit.edu
"""

import tensorflow as tf
import tensorflow.contrib.slim as slim
from tensorflow.python import debug as tf_debug # for debugging
import numpy as np

import time
import sys
import os
import re   # for regular expressions

from config import FLAGS
import config as conf
import models as mod
import metrics as met
import utils as utl
import math

# Get logger that has already been created in config.py
import daiquiri
logger = daiquiri.getLogger(__name__)

# for logging detached images
from io import BytesIO
import matplotlib.pyplot as plt
from PIL import Image


def main(args):
  """Run training and validation.
  
  1. Build graphs
      1.1 Training graph to run on multiple GPUs
      1.2 Validation graph to run on multiple GPUs
  2. Configure sessions
      2.1 Train
      2.2 Validate
  3. Main loop
      3.1 Train
      3.2 Write summary
      3.3 Save model
      3.4 Validate model
      
  Author:
    Perry Deng
  """
  
  # Set reproduciable random seed
  tf.set_random_seed(1234)
    
  # Directories
  train_dir, train_summary_dir = conf.setup_train_directories()
  
  # Logger
  conf.setup_logger(logger_dir=train_dir, name="logger_train.txt")
  
  # Hyperparameters
  conf.load_or_save_hyperparams(train_dir)
  
  # Get dataset hyperparameters
  logger.info('Using dataset: {}'.format(FLAGS.dataset))
  dataset_size_train = conf.get_dataset_size_train(FLAGS.dataset)\
      if not FLAGS.train_on_test else conf.get_dataset_size_test(FLAGS.dataset)
  dataset_size_val = conf.get_dataset_size_validate(FLAGS.dataset)
  build_arch = conf.get_dataset_architecture(FLAGS.dataset)
  num_classes = conf.get_num_classes(FLAGS.dataset)
  create_inputs_train = conf.get_create_inputs(FLAGS.dataset, mode="train_whole")\
      if not FLAGS.train_on_test else conf.get_create_inputs(FLAGS.dataset, mode="train_on_test")
  create_inputs_train_wholeset = conf.get_create_inputs(FLAGS.dataset, mode="train_whole")
  if dataset_size_val > 0:
    create_inputs_val   = conf.get_create_inputs(FLAGS.dataset, mode="validate")

  
 #*****************************************************************************
 # 1. BUILD GRAPHS
 #*****************************************************************************

  #----------------------------------------------------------------------------
  # GRAPH - TRAIN
  #----------------------------------------------------------------------------
  logger.info('BUILD TRAIN GRAPH')
  g_train = tf.Graph()
  with g_train.as_default(), tf.device('/cpu:0'):
    
    # Get global_step
    global_step = tf.train.get_or_create_global_step()

    # Get batches per epoch
    num_batches_per_epoch = int(dataset_size_train / FLAGS.batch_size)

    # In response to a question on OpenReview, Hinton et al. wrote the 
    # following:
    # "We use an exponential decay with learning rate: 3e-3, decay_steps: 20000,     # decay rate: 0.96."
    # https://openreview.net/forum?id=HJWLfGWRb&noteId=ryxTPFDe2X
    lrn_rate = tf.train.exponential_decay(learning_rate = FLAGS.lrn_rate, 
                        global_step = global_step,
                        decay_steps = 20000,
                        decay_rate = 0.96)
    tf.summary.scalar('learning_rate', lrn_rate)
    opt = tf.train.AdamOptimizer(learning_rate=lrn_rate)

    # Get batch from data queue. Batch size is FLAGS.batch_size, which is then 
    # divided across multiple GPUs
    input_dict = create_inputs_train()
    batch_x = input_dict['image']
    batch_labels = input_dict['label']
    
    # AG 03/10/2018: Split batch for multi gpu implementation
    # Each split is of size FLAGS.batch_size / FLAGS.num_gpus
    # See: https://github.com/naturomics/CapsNet-Tensorflow/blob/master/
    # dist_version/distributed_train.py
    splits_x = tf.split(
        axis=0, 
        num_or_size_splits=FLAGS.num_gpus, 
        value=batch_x)
    splits_labels = tf.split(
        axis=0, 
        num_or_size_splits=FLAGS.num_gpus, 
        value=batch_labels)

    
    #--------------------------------------------------------------------------
    # MULTI GPU - TRAIN
    #--------------------------------------------------------------------------
    # Calculate the gradients for each model tower
    tower_grads = []
    tower_losses = []
    tower_logits = []
    tower_target_labels = []
    reuse_variables = None
    for i in range(FLAGS.num_gpus):
      with tf.device('/gpu:%d' % i):
        with tf.name_scope('tower_%d' % i) as scope:
          logger.info('TOWER %d' % i)
          #with slim.arg_scope([slim.model_variable, slim.variable],
          # device='/cpu:0'):
          with slim.arg_scope([slim.variable], device='/cpu:0'):
            loss, logits, x, patch, target_labels = tower_fn(
                build_arch,
                splits_x[i],
                splits_labels[i],
                scope,
                num_classes,
                reuse_variables=reuse_variables,
                is_train=True)
          
          # Don't reuse variable for first GPU, but do reuse for others
          reuse_variables = True
          
          # Compute gradients for one GPU
          patch_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                           "patch_params")
          grads = opt.compute_gradients(loss, var_list=patch_params)
          
          # Keep track of the gradients across all towers.
          tower_grads.append(grads)
          tower_target_labels.append(target_labels)          

          # Keep track of losses and logits across for each tower
          tower_logits.append(logits)
          tower_losses.append(loss)
          
          # Loss for each tower
          tf.summary.scalar("loss", loss)
    
    # We must calculate the mean of each gradient. Note that this is the
    # synchronization point across all towers.
    grads = average_gradients(tower_grads)
    
    # See: https://stackoverflow.com/questions/40701712/how-to-check-nan-in-
    # gradients-in-tensorflow-when-updating
    grad_check = ([tf.check_numerics(g, message='Gradient NaN Found!') 
                      for g, _ in grads if g is not None]
                  + [tf.check_numerics(loss, message='Loss NaN Found')])
    
    # Apply the gradients to adjust the shared variables
    with tf.control_dependencies(grad_check):
      update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
      with tf.control_dependencies(update_ops):
        train_op = opt.apply_gradients(grads, global_step=global_step)
    
    # Calculate mean loss     
    loss = tf.reduce_mean(tower_losses)
    
    # Calculate accuracy
    logits = tf.concat(tower_logits, axis=0)
    target_labels = tf.concat(tower_target_labels, axis=0)
    acc = met.accuracy(logits, target_labels)
    
    # Prepare predictions and one-hot labels
    probs = tf.nn.softmax(logits=logits)
    labels_oh = tf.one_hot(batch_labels, num_classes)
    
    # Group metrics together
    # See: https://cs230-stanford.github.io/tensorflow-model.html
    trn_metrics = {'loss' : loss,
             'labels' : batch_labels, 
             'labels_oh' : labels_oh,
             'logits' : logits,
             'probs' : probs,
             'acc' : acc,
             }
    
    # Reset and read operations for streaming metrics go here
    trn_reset = {}
    trn_read = {}
    
    # Logging
    tf.summary.scalar('batch_loss', loss)
    tf.summary.scalar('batch_success_rate', acc)

    # Set Saver
    # AG 26/09/2018: Save all variables including Adam so that we can continue 
    # training from where we left off
    # max_to_keep=None should keep all checkpoints
    saver = tf.train.Saver(tf.global_variables(), max_to_keep=None)
    
    # Display number of parameters
    train_params = np.sum([np.prod(v.get_shape().as_list())
              for v in tf.trainable_variables()]).astype(np.int32)
    logger.info('Trainable Parameters: {}'.format(train_params))
        
    # Set summary op
    trn_summary = tf.summary.merge_all()

  #----------------------------------------------------------------------------
  # GRAPH - TRAINING SET ACCURACY
  #----------------------------------------------------------------------------
  logger.info('BUILD TRAINING SET ACCURACY GRAPH')
  g_trn_acc = tf.Graph()
  with g_trn_acc.as_default():
    # Get global_step
    global_step = tf.train.get_or_create_global_step()

    
    # Get data
    input_dict = create_inputs_train_wholeset()
    batch_x = input_dict['image']
    batch_labels = input_dict['label']
    
    # AG 10/12/2018: Split batch for multi gpu implementation
    # Each split is of size FLAGS.batch_size / FLAGS.num_gpus
    # See: https://github.com/naturomics/CapsNet-
    # Tensorflow/blob/master/dist_version/distributed_train.py
    splits_x = tf.split(
        axis=0, 
        num_or_size_splits=FLAGS.num_gpus, 
        value=batch_x)
    splits_labels = tf.split(
        axis=0, 
        num_or_size_splits=FLAGS.num_gpus, 
        value=batch_labels)
    
    
    #--------------------------------------------------------------------------
    # MULTI GPU - TRAINING SET ACCURACY
    #--------------------------------------------------------------------------
    # Calculate the logits for each model tower
    tower_logits = []
    tower_target_labels = []
    reuse_variables = None
    for i in range(FLAGS.num_gpus):
      with tf.device('/gpu:%d' % i):
        with tf.name_scope('tower_%d' % i) as scope:
          with slim.arg_scope([slim.variable], device='/cpu:0'):
            loss, logits, x, patch, target_labels = tower_fn(
                build_arch, 
                splits_x[i], 
                splits_labels[i], 
                scope, 
                num_classes, 
                reuse_variables=reuse_variables, 
                is_train=False)

          # Don't reuse variable for first GPU, but do reuse for others
          reuse_variables = True
          
          # Keep track of losses and logits across for each tower
          tower_logits.append(logits)
          tower_target_labels.append(target_labels)
          # Loss for each tower
          tf.summary.histogram("train_set_logits", logits)
    
    # Combine logits from all towers
    logits = tf.concat(tower_logits, axis=0)
    target_labels = tf.concat(tower_target_labels, axis=0)
    # Calculate metrics
    train_set_loss = mod.spread_loss(logits, target_labels)
    train_set_acc = met.accuracy(logits, target_labels)
    
    # Prepare predictions and one-hot labels
    train_set_probs = tf.nn.softmax(logits=logits)
    train_set_labels_oh = tf.one_hot(batch_labels, num_classes)
    
    # Group metrics together
    # See: https://cs230-stanford.github.io/tensorflow-model.html
    train_set_metrics = {'loss' : train_set_loss,
                   'labels' : batch_labels, 
                   'labels_oh' : train_set_labels_oh,
                   'logits' : logits,
                   'probs' : train_set_probs,
                   'acc' : train_set_acc,
                   }
    
    # Reset and read operations for streaming metrics go here
    train_set_reset = {}
    train_set_read = {}
    saver = tf.train.Saver(max_to_keep=None)
    
    tf.summary.scalar("train_set_loss", train_set_loss)
    tf.summary.scalar("train_set_success_rate", train_set_acc)
    trn_acc_summary = tf.summary.merge_all()
  
  if dataset_size_val > 0: 
    #----------------------------------------------------------------------------
    # GRAPH - VALIDATION
    #----------------------------------------------------------------------------
    logger.info('BUILD VALIDATION GRAPH')
    g_val = tf.Graph()
    with g_val.as_default():
      # Get global_step
      global_step = tf.train.get_or_create_global_step()

      num_batches_val = int(dataset_size_val / FLAGS.batch_size)
      
      # Get data
      input_dict = create_inputs_val()
      batch_x = input_dict['image']
      batch_labels = input_dict['label']
      
      # AG 10/12/2018: Split batch for multi gpu implementation
      # Each split is of size FLAGS.batch_size / FLAGS.num_gpus
      # See: https://github.com/naturomics/CapsNet-
      # Tensorflow/blob/master/dist_version/distributed_train.py
      splits_x = tf.split(
          axis=0, 
          num_or_size_splits=FLAGS.num_gpus, 
          value=batch_x)
      splits_labels = tf.split(
          axis=0, 
          num_or_size_splits=FLAGS.num_gpus, 
          value=batch_labels)
      
      
      #--------------------------------------------------------------------------
      # MULTI GPU - VALIDATE
      #--------------------------------------------------------------------------
      # Calculate the logits for each model tower
      tower_logits = []
      tower_target_labels = []
      reuse_variables = None
      for i in range(FLAGS.num_gpus):
        with tf.device('/gpu:%d' % i):
          with tf.name_scope('tower_%d' % i) as scope:
            with slim.arg_scope([slim.variable], device='/cpu:0'):
              loss, logits, x, patch, target_labels = tower_fn(
                  build_arch, 
                  splits_x[i], 
                  splits_labels[i], 
                  scope, 
                  num_classes, 
                  reuse_variables=reuse_variables, 
                  is_train=False)

            # Don't reuse variable for first GPU, but do reuse for others
            reuse_variables = True
            
            # Keep track of losses and logits across for each tower
            tower_logits.append(logits)
            tower_target_labels.append(target_labels)
            # Loss for each tower
            tf.summary.histogram("val_logits", logits)

      # take patch and patched images from last tower
      val_patch = patch
      val_x = x

      # Combine logits from all towers
      logits = tf.concat(tower_logits, axis=0)
      target_labels = tf.concat(tower_target_labels, axis=0)
      # Calculate metrics
      val_loss = mod.spread_loss(logits, target_labels)
      val_acc = met.accuracy(logits, target_labels)
      
      # Prepare predictions and one-hot labels
      val_probs = tf.nn.softmax(logits=logits)
      val_labels_oh = tf.one_hot(batch_labels, num_classes)
      
      # Group metrics together
      # See: https://cs230-stanford.github.io/tensorflow-model.html
      val_metrics = {'loss' : val_loss,
                     'labels' : batch_labels, 
                     'labels_oh' : val_labels_oh,
                     'logits' : logits,
                     'probs' : val_probs,
                     'acc' : val_acc,
                     }
      val_images = {'patch' : val_patch,
                    'x' : val_x} 
      # Reset and read operations for streaming metrics go here
      val_reset = {}
      val_read = {}
      
      tf.summary.scalar("val_loss", val_loss)
      tf.summary.scalar("val_success_rate", val_acc)
        
      # Saver
      saver = tf.train.Saver(max_to_keep=1)
      
      # Set summary op
      val_summary = tf.summary.merge_all()
       
        
  #****************************************************************************
  # 2. SESSIONS
  #****************************************************************************
          
  #----- SESSION TRAIN -----#
  # Session settings
  #sess_train = tf.Session(config=tf.ConfigProto(allow_soft_placement=True,
  #                                              log_device_placement=False),
  #                        graph=g_train)

  # Perry: added in for RTX 2070 incompatibility workaround
  config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
  config.gpu_options.allow_growth = True
  sess_train = tf.Session(config=config, graph=g_train)

  # Debugger
  # AG 05/06/2018: Debugging using either command line or TensorBoard
  if FLAGS.debugger is not None:
    # sess = tf_debug.LocalCLIDebugWrapperSession(sess)
    sess_train = tf_debug.TensorBoardDebugWrapperSession(sess_train, 
                                                         FLAGS.debugger)
    
  with g_train.as_default():
    sess_train.run([tf.global_variables_initializer(),
                    tf.local_variables_initializer()])
    
    # Restore previous checkpoint
    # AG 26/09/2018: where should this go???
    if FLAGS.load_dir is not None:
      prev_step = load_training(saver, sess_train, FLAGS.load_dir, opt)
    else:
      prev_step = 0

  # Create summary writer, and write the train graph
  summary_writer = tf.summary.FileWriter(train_summary_dir, 
                                         graph=sess_train.graph)


  #----- SESSION TRAIN SET ACCURACY -----#
  #sess_val = tf.Session(config=tf.ConfigProto(allow_soft_placement=True,
  #                                            log_device_placement=False),
  #                      graph=g_val)

  # Perry: added in for RTX 2070 incompatibility workaround
  config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
  config.gpu_options.allow_growth = True
  sess_train_acc = tf.Session(config=config, graph=g_trn_acc)

  with g_trn_acc.as_default():
    sess_train_acc.run([tf.local_variables_initializer(), 
                        tf.global_variables_initializer()])


  if dataset_size_val > 0:
    #----- SESSION VALIDATION -----#
    #sess_val = tf.Session(config=tf.ConfigProto(allow_soft_placement=True,
    #                                            log_device_placement=False),
    #                      graph=g_val)
 
    # Perry: added in for RTX 2070 incompatibility workaround
    config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
    config.gpu_options.allow_growth = True
    sess_val = tf.Session(config=config, graph=g_val)


    with g_val.as_default():
      sess_val.run([tf.local_variables_initializer(), 
                    tf.global_variables_initializer()])


  #****************************************************************************
  # 3. MAIN LOOP
  #****************************************************************************
  SUMMARY_FREQ = 100
  SAVE_MODEL_FREQ = num_batches_per_epoch # 500
  VAL_FREQ = num_batches_per_epoch # 500
  PROFILE_FREQ = 5
  #print("starting main loop") 
  for step in range(prev_step, FLAGS.epoch * num_batches_per_epoch + 1): 
    #print("looping")
  #for step in range(0,3):
    # AG 23/05/2018: limit number of iterations for testing
    # for step in range(100):
    epoch_decimal = step/num_batches_per_epoch
    epoch = int(np.floor(epoch_decimal))
    

    # TF queue would pop batch until no file
    try: 
      # TRAIN
      with g_train.as_default():
    
          # With profiling
          if (FLAGS.profile is True) and ((step % PROFILE_FREQ) == 0): 
            logger.info("Train with Profiling")
            run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
            run_metadata = tf.RunMetadata()
          # Without profiling
          else:
            run_options = None
            run_metadata = None
          
          # Reset streaming metrics
          if step % (num_batches_per_epoch/4) == 1:
            logger.info("Reset streaming metrics")
            sess_train.run([trn_reset])
          
          # MAIN RUN
          tic = time.time()
          train_op_v, trn_metrics_v, trn_summary_v = sess_train.run(
              [train_op, trn_metrics, trn_summary], 
              options=run_options, 
              run_metadata=run_metadata)
          toc = time.time()
          
          # Read streaming metrics
          trn_read_v = sess_train.run(trn_read)
          
          # Write summary for profiling
          if run_options is not None: 
            summary_writer.add_run_metadata(
                run_metadata, 'epoch{:f}'.format(epoch_decimal))
          
          # Logging
          #logger.info('TRN'
          #      + ' e-{:d}'.format(epoch)
          #      + ' stp-{:d}'.format(step) 
          #        )
          #      + ' {:.2f}s'.format(toc - tic) 
          #      + ' loss: {:.4f}'.format(trn_metrics_v['loss'])
          #      + ' acc: {:.2f}%'.format(trn_metrics_v['acc']*100)
          #       )

    except KeyboardInterrupt:
      sess_train.close()
      sess_val.close()
      sys.exit()
      
    except tf.errors.InvalidArgumentError as e:
      logger.warning('%d iteration contains NaN gradients. Discard.' % step)
      logger.error(str(e))
      continue
      
    else:
      # WRITE SUMMARY
      if (step % SUMMARY_FREQ) == 0:
        logger.info("Write Train Summary")
        with g_train.as_default():
          # Summaries from graph
          summary_writer.add_summary(trn_summary_v, step)
          
      # SAVE MODEL
      if (step % SAVE_MODEL_FREQ) == 0:
        logger.info("Save Model")
        with g_train.as_default():
          train_checkpoint_dir = train_dir + '/checkpoint'
          if not os.path.exists(train_checkpoint_dir):
            os.makedirs(train_checkpoint_dir)

          # Save ckpt from train session
          ckpt_path = os.path.join(train_checkpoint_dir, 'model.ckpt' + str(epoch))
          saver.save(sess_train, ckpt_path, global_step=step)
      if (step % VAL_FREQ) == 0:
        # calculate metrics every epoch
        with g_trn_acc.as_default():
          logger.info("Start Train Set Accuracy")
          # Restore ckpt to val session
          latest_ckpt = tf.train.latest_checkpoint(train_checkpoint_dir)
          saver.restore(sess_train_acc, latest_ckpt)
          
          # Reset accumulators
          accuracy_sum = 0
          loss_sum = 0
          sess_train_acc.run(train_set_reset)
          
          for i in range(num_batches_per_epoch):
            train_set_metrics_v, train_set_summary_str_v = sess_train_acc.run(
                [train_set_metrics, trn_acc_summary])
            
            # Update
            accuracy_sum += train_set_metrics_v['acc']
            loss_sum += train_set_metrics_v['loss']
            
            # Read
            trn_read_v = sess_train_acc.run(val_read)
            
            # Get checkpoint number
            ckpt_num = re.split('-', latest_ckpt)[-1]

          # Average across batches
          ave_acc = accuracy_sum / num_batches_per_epoch
          ave_loss = loss_sum / num_batches_per_epoch
           
          logger.info('TRN ckpt-{}'.format(ckpt_num) 
                      + ' avg_success: {:.2f}%'.format(ave_acc*100) 
                      + ' avg_loss: {:.4f}'.format(ave_loss)
                     )
          
          logger.info("Write Train Summary")
          summary_train = tf.Summary()
          summary_train.value.add(tag="trn_success", simple_value=ave_acc)
          summary_train.value.add(tag="trn_loss", simple_value=ave_loss)
          summary_writer.add_summary(summary_train, epoch)
          

        if dataset_size_val > 0: 
          #----- Validation -----#
          with g_val.as_default():
            logger.info("Start Validation")
            
            # Restore ckpt to val session
            latest_ckpt = tf.train.latest_checkpoint(train_checkpoint_dir)
            saver.restore(sess_val, latest_ckpt)
            
            # Reset accumulators
            accuracy_sum = 0
            loss_sum = 0
            sess_val.run(val_reset)
            
            for i in range(num_batches_val):
              if i == num_batches_val - 1:
                # take a sample of patched images on the last validation batch
                val_metrics_v, val_summary_str_v, val_images_v = sess_val.run(
                    [val_metrics, val_summary, val_images])
                x = val_images_v['x']
                patch = val_images_v['patch']
              else:
                val_metrics_v, val_summary_str_v = sess_val.run(
                    [val_metrics, val_summary])
              # Update
              accuracy_sum += val_metrics_v['acc']
              loss_sum += val_metrics_v['loss']
              
              # Read
              val_read_v = sess_val.run(val_read)
              
              # Get checkpoint number
              ckpt_num = re.split('-', latest_ckpt)[-1]

              # Logging
              #logger.info('VAL ckpt-{}'.format(ckpt_num) 
              #            + ' bch-{:d}'.format(i) 
              #            + ' cum_acc: {:.2f}%'.format(accuracy_sum/(i+1)*100) 
              #            + ' cum_loss: {:.4f}'.format(loss_sum/(i+1))
              #           )
            
            # Average across batches
            ave_acc = accuracy_sum / num_batches_val
            ave_loss = loss_sum / num_batches_val
             
            logger.info('VAL ckpt-{}'.format(ckpt_num) 
                        + ' avg_success: {:.2f}%'.format(ave_acc*100) 
                        + ' avg_loss: {:.4f}'.format(ave_loss)
                       )
            
            logger.info("Write Val Summary")
            summary_val = tf.Summary()
            summary_val.value.add(tag="val_success", simple_value=ave_acc)
            summary_val.value.add(tag="val_loss", simple_value=ave_loss)
            summary_writer.add_summary(summary_val, epoch)
            log_images(summary_writer, "patch", [patch], epoch)
            log_images(summary_writer, "patched_input", x, epoch)
            if patch.shape[-1] == 1:
              patch = np.squeeze(patch, axis=-1)
            formatted = (patch * 255).astype('uint8')
            img = Image.fromarray(formatted)
            img.save(os.path.join(train_dir, "saved_patch.png"))
 
  # Close (main loop)
  sess_train.close()
  sess_val.close()
  sys.exit()

def _pad_and_tile_patch(patch):
  """
  https://github.com/tensorflow/cleverhans/blob/master/examples/adversarial_patch/AdversarialPatch.ipynb
  :param patch:
  :return:
  """
  # Calculate the exact padding
  # Image shape req'd because it is sometimes 299 sometimes 224

  # padding is the amount of space available on either side of the centered patch
  # WARNING: This has been integer-rounded and could be off by one.
  #          See _pad_and_tile_patch for usage
  return tf.stack([patch] * FLAGS.batch_size)


def _transform_vector(width, x_shift, y_shift, im_scale, rot_in_degrees):
  """
  https://github.com/tensorflow/cleverhans/blob/master/examples/adversarial_patch/AdversarialPatch.ipynb
   If one row of transforms is [a0, a1, a2, b0, b1, b2, c0, c1],
   then it maps the output point (x, y) to a transformed input point
   (x', y') = ((a0 x + a1 y + a2) / k, (b0 x + b1 y + b2) / k),
   where k = c0 x + c1 y + 1.
   The transforms are inverted compared to the transform mapping input points to output points.
  """

  rot = float(rot_in_degrees) / 90. * (math.pi / 2)

  # Standard rotation matrix
  # (use negative rot because tf.contrib.image.transform will do the inverse)
  rot_matrix = np.array(
    [[math.cos(-rot), -math.sin(-rot)],
     [math.sin(-rot), math.cos(-rot)]]
  )

  # Scale it
  # (use inverse scale because tf.contrib.image.transform will do the inverse)
  epsilon = 1e-8
  inv_scale = 1. / (im_scale + epsilon)
  xform_matrix = rot_matrix * inv_scale
  a0, a1 = xform_matrix[0]
  b0, b1 = xform_matrix[1]

  # At this point, the image will have been rotated around the top left corner,
  # rather than around the center of the image.
  #
  # To fix this, we will see where the center of the image got sent by our transform,
  # and then undo that as part of the translation we apply.
  x_origin = float(width) / 2
  y_origin = float(width) / 2

  x_origin_shifted, y_origin_shifted = np.matmul(
    xform_matrix,
    np.array([x_origin, y_origin]),
  )

  x_origin_delta = x_origin - x_origin_shifted
  y_origin_delta = y_origin - y_origin_shifted

  # Combine our desired shifts with the rotation-induced undesirable shift
  a2 = x_origin_delta - (x_shift / (2 * im_scale + epsilon))
  b2 = y_origin_delta - (y_shift / (2 * im_scale + epsilon))

  # Return these values in the order that tf.contrib.image.transform expects
  return np.array([a0, a1, a2, b0, b1, b2, 0, 0]).astype(np.float32)


def _circle_mask(shape, sharpness = 40):
  """Return a circular mask of a given shape
  https://github.com/tensorflow/cleverhans/blob/master/examples/adversarial_patch/AdversarialPatch.ipynb"""
  assert shape[0] == shape[1], "circle_mask received a bad shape: " + shape

  diameter = shape[0]
  x = np.linspace(-1, 1, diameter)
  y = np.linspace(-1, 1, diameter)
  xx, yy = np.meshgrid(x, y, sparse=True)
  z = (xx**2 + yy**2) ** sharpness

  mask = 1 - np.clip(z, -1, 1)
  mask = np.expand_dims(mask, axis=2)
  mask = np.broadcast_to(mask, shape).astype(np.float32)
  return mask


def _random_overlay(imgs, patch, scale_min, scale_max):
  """Augment images with randomly transformed patch.
  https://github.com/tensorflow/cleverhans/blob/master/examples/adversarial_patch/AdversarialPatch.ipynb

  """
  # Add padding
  batch_size = imgs.get_shape().as_list()[0]
  max_rotation = FLAGS.max_rotation
  image_shape = imgs.get_shape().as_list()[1:]

  image_mask = _circle_mask(image_shape)
  image_mask = tf.stack([image_mask] * batch_size)
  padded_patch = tf.stack([patch] * batch_size)

  def _random_transformation(scale_mi, scale_ma, width):
    im_scale = np.random.uniform(low=scale_mi, high=scale_ma)

    padding_after_scaling = (1 - im_scale) * width
    x_delta = np.random.uniform(-padding_after_scaling, padding_after_scaling)
    y_delta = np.random.uniform(-padding_after_scaling, padding_after_scaling)

    rot = np.random.uniform(-max_rotation, max_rotation)

    return _transform_vector(width,
                             x_shift=x_delta,
                             y_shift=y_delta,
                             im_scale=im_scale,
                             rot_in_degrees=rot)
  transform_vecs = []
  for _ in range(batch_size):
    random_xform_vector = tf.py_func(_random_transformation, [scale_min, scale_max, image_shape[0]],
                                     tf.float32)
    random_xform_vector.set_shape([8])
    transform_vecs.append(random_xform_vector)

  image_mask = tf.contrib.image.transform(image_mask, transform_vecs, "BILINEAR")
  padded_patch = tf.contrib.image.transform(padded_patch, transform_vecs, "BILINEAR")

  inverted_mask = (1 - image_mask)
  return imgs * inverted_mask + padded_patch * image_mask


def patch_inputs(x, is_train=True, reuse=None,
                 scale_min=FLAGS.scale_min, scale_max=FLAGS.scale_max,
                 patch_feed=None):
  with tf.variable_scope(tf.get_variable_scope(), reuse=reuse):
    # for this implementation, patch will have the same resolution as input
    patch_shape = x.get_shape().as_list()[1:]
    if patch_feed is None:
      patch_params = tf.get_variable("patch_params", patch_shape, trainable=is_train,
                                      initializer=tf.random_uniform_initializer(minval=-2, maxval=2),
                                      regularizer=None)
      # box constraint the patch scalars into (0, 1) using change of variables approach
      patch_node = tf.math.scalar_mul(0.5, tf.math.add(tf.math.tanh(patch_params), 1), name="default_patch")
    else:
      patch_node = patch_feed
    patched_x = _random_overlay(x, patch_node, scale_min, scale_max)
    patched_x = tf.clip_by_value(patched_x, clip_value_min=0, clip_value_max=1)
  return patched_x, patch_node


def tower_fn(build_arch,
             x,
             y,
             scope,
             num_classes,
             is_train=True,
             reuse_variables=None):
  """Model tower to be run on each GPU.
  
  Args: 
    build_arch:
    x: split of batch_x allocated to particular GPU
    y: split of batch_y allocated to particular GPU
    scope:
    num_classes:
    is_train:
    reuse_variables: False for the first GPU, and True for subsequent GPUs

  Returns:
    loss: mean loss across samples for one tower (scalar)
    scores: 
      If the architecture is a capsule network, then the scores are the output 
      activations of the class caps.
      If the architecture is the CNN baseline, then the scores are the logits of 
      the final layer.
      (samples_per_tower, n_classes)
      (64/4=16, 5)
  """
  
  with tf.variable_scope(tf.get_variable_scope(), reuse=reuse_variables):
    x, patch = patch_inputs(x, is_train=is_train, reuse=reuse_variables)
    output = build_arch(x, False, num_classes=num_classes)
  targets = tf.fill(dims=y.get_shape().as_list(), value=FLAGS.target_class, name="adversarial_targets")
  if FLAGS.carliniwagner:
    # carlini wagner adversarial objective
    loss = mod.carlini_wagner_loss(output, targets, num_classes)
  else:
    # default hinge loss from Hinton et al
    loss = mod.total_loss(output, targets)
  return loss, output['scores'], x, patch, targets


def average_gradients(tower_grads):
  """Compute average gradients across all towers.
  
  Calculate the average gradient for each shared variable across all towers.
  Note that this function provides a synchronization point across all towers.
  
  Credit:
    https://github.com/naturomics/CapsNet-
    Tensorflow/blob/master/dist_version/distributed_train.py
  Args:
    tower_grads: 
      List of lists of (gradient, variable) tuples. The outer list is over 
      individual gradients. The inner list is over the gradient calculation for       each tower.
  Returns:
    average_grads:
      List of pairs of (gradient, variable) where the gradient has been 
      averaged across all towers.
  """
  
  average_grads = []
  for grad_and_vars in zip(*tower_grads):
  # Note that each grad_and_vars looks like the following:
  #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
    grads = []
    for g, _ in grad_and_vars:
      # Add 0 dimension to the gradients to represent the tower.
      expanded_g = tf.expand_dims(g, 0)

      # Append on a 'tower' dimension which we will average over below.
      grads.append(expanded_g)

    # Average over the 'tower' dimension.
    grad = tf.concat(axis=0, values=grads)
    grad = tf.reduce_mean(grad, 0)

    # Keep in mind that the Variables are redundant because they are shared
    # across towers. So .. we will just return the first tower's pointer to
    # the Variable.
    v = grad_and_vars[0][1]
    grad_and_var = (grad, v)
    average_grads.append(grad_and_var)
    
  return average_grads
          

def extract_step(path):
  """Returns the step from the file format name of Tensorflow checkpoints.
  
  Credit:
    Sara Sabour
    https://github.com/Sarasra/models/blob/master/research/capsules/
    experiment.py
  Args:
    path: The checkpoint path returned by tf.train.get_checkpoint_state.
    The format is: {ckpnt_name}-{step}
  Returns:
    The last training step number of the checkpoint.
  """
  file_name = os.path.basename(path)
  return int(file_name.split('-')[-1])


def load_training(saver, session, load_dir, optimizer=None):
  """Loads a saved model into current session or initializes the directory.
  
  If there is no functioning saved model or FLAGS.restart is set, cleans the
  load_dir directory. Otherwise, loads the latest saved checkpoint in load_dir
  to session.
  
  Author:
    Ashley Gritzman 26/09/2018
  Credit:
    Adapted from Sara Sabour
    https://github.com/Sarasra/models/blob/master/research/capsules/
    experiment.py
  Args:
    saver: An instance of tf.train.saver to load the model in to the session.
    session: An instance of tf.Session with the built-in model graph.
    load_dir: The directory which is used to load the latest checkpoint.
    
  Returns:
    The latest saved step.
  """
  checkpoint_dir = os.path.join(load_dir, "train", "checkpoint")
  if tf.gfile.Exists(checkpoint_dir):
    ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
    if ckpt and ckpt.model_checkpoint_path:
      restored_variables = tf.get_collection_ref(tf.GraphKeys.GLOBAL_VARIABLES)
      prev_step = extract_step(ckpt.model_checkpoint_path)
      if FLAGS.new_patch:
        restored_variables = [v for v in restored_variables if "patch_params" not in v.name]
        for optim_var in optimizer.variables():
          excluded = optim_var.name
          restored_variables = [v for v in restored_variables if excluded not in v.name]
        saver = tf.train.Saver(restored_variables, max_to_keep=None)
        prev_step = 0
      saver.restore(session, ckpt.model_checkpoint_path)
      logger.info("Restored checkpoint")
    else:
      raise IOError("""AG: load_ckpt directory exists but cannot find a valid 
                    checkpoint to restore, consider using the reset flag""")
  else:
    raise IOError("AG: load_ckpt directory does not exist")
    
  return prev_step


def find_checkpoint(load_dir, seen_step):
  """Finds the global step for the latest written checkpoint to the load_dir.
  
  Credit:
    Sara Sabour
    https://github.com/Sarasra/models/blob/master/research/capsules/
    experiment.py
  Args:
    load_dir: The directory address to look for the training checkpoints.
    seen_step: Latest step which evaluation has been done on it.
  Returns:
    The latest new step in the load_dir and the file path of the latest model
    in load_dir. If no new file is found returns -1 and None.
  """
  ckpt = tf.train.get_checkpoint_state(load_dir)
  if ckpt and ckpt.model_checkpoint_path:
    global_step = extract_step(ckpt.model_checkpoint_path)
    if int(global_step) != seen_step:
      return int(global_step), ckpt.model_checkpoint_path
  return -1, None


def log_images(writer, tag, images, step, bound=8):
   """logs a list of detached images."""

   im_summaries = []
   for nr, img in enumerate(images):
     if nr == bound:
       break
     # Write the image to a string
     buffr = BytesIO()
     if len(img.shape) == 3 and img.shape[2] == 1:
       img = np.squeeze(img, axis=-1)
     plt.imsave(buffr, img, vmin=0, vmax=1, format='png')

     # Create an Image object
     img_sum = tf.Summary.Image(encoded_image_string=buffr.getvalue(),
                                height=img.shape[0],
                                width=img.shape[1])
     # Create a Summary value
     im_summaries.append(tf.Summary.Value(tag='%s/%d' % (tag, nr),
                                          image=img_sum))

   # Create and write Summary
   summary = tf.Summary(value=im_summaries)
   writer.add_summary(summary, step)          


if __name__ == "__main__":
  tf.app.run()
