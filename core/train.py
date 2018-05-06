#!/usr/bin/python
# -*- coding: utf-8 -*-
# 
# Developed by Haozhe Xie <cshzxie@gmail.com>

import matplotlib.pyplot as plt
import numpy as np
import os
import torch
import torch.backends.cudnn
import torch.utils.data

import utils.binvox_visualization
import utils.data_loaders
import utils.data_transforms
import utils.network_utils

from datetime import datetime as dt
from tensorboardX import SummaryWriter
from time import time

from core.test import test_net
from models.generator import Generator
from models.image_encoder import ImageEncoder

def train_net(cfg):
    # Enable the inbuilt cudnn auto-tuner to find the best algorithm to use
    torch.backends.cudnn.benchmark  = True

    # Set up data augmentation
    IMG_SIZE  = cfg.CONST.IMG_H, cfg.CONST.IMG_W
    CROP_SIZE = cfg.TRAIN.CROP_IMG_H, cfg.TRAIN.CROP_IMG_W
    train_transforms = utils.data_transforms.Compose([
        utils.data_transforms.Normalize(mean=cfg.DATASET.MEAN, std=cfg.DATASET.STD),
        utils.data_transforms.RandomCrop(IMG_SIZE, CROP_SIZE),
        utils.data_transforms.RandomBackground(cfg.TRAIN.RANDOM_BG_COLOR_RANGE),
        utils.data_transforms.ColorJitter(cfg.TRAIN.BRIGHTNESS, cfg.TRAIN.CONTRAST, cfg.TRAIN.SATURATION, cfg.TRAIN.HUE),
        utils.data_transforms.ToTensor(),
    ])
    val_transforms  = utils.data_transforms.Compose([
        utils.data_transforms.Normalize(mean=cfg.DATASET.MEAN, std=cfg.DATASET.STD),
        utils.data_transforms.CenterCrop(IMG_SIZE, CROP_SIZE),
        utils.data_transforms.RandomBackground(cfg.TEST.RANDOM_BG_COLOR_RANGE),
        utils.data_transforms.ToTensor(),
    ])
    
    # Set up data loader
    dataset_loader    = utils.data_loaders.DATASET_LOADER_MAPPING[cfg.DATASET.DATASET_NAME](cfg)
    n_views           = np.random.randint(cfg.CONST.N_VIEWS) + 1 if cfg.TRAIN.RANDOM_NUM_VIEWS else cfg.CONST.N_VIEWS
    train_data_loader = torch.utils.data.DataLoader(
        dataset=dataset_loader.get_dataset(cfg.TRAIN.DATASET_PORTION, n_views, train_transforms),
        batch_size=cfg.CONST.BATCH_SIZE,
        num_workers=cfg.TRAIN.NUM_WORKER, pin_memory=True, shuffle=True)
    val_data_loader = torch.utils.data.DataLoader(
        dataset=dataset_loader.get_dataset(cfg.TEST.DATASET_PORTION, n_views, val_transforms),
        batch_size=1,
        num_workers=1, pin_memory=True, shuffle=False)

    # Summary writer for TensorBoard
    output_dir   = os.path.join(cfg.DIR.OUT_PATH, '%s', dt.now().isoformat())
    log_dir      = output_dir % 'logs'
    img_dir      = output_dir % 'images'
    ckpt_dir     = output_dir % 'checkpoints'
    train_writer = SummaryWriter(os.path.join(log_dir, 'train'))
    val_writer   = SummaryWriter(os.path.join(log_dir, 'test'))

    # Set up networks
    generator            = Generator(cfg)
    image_encoder        = ImageEncoder(cfg)

    # Initialize weights of networks
    generator.apply(utils.network_utils.init_weights)
    image_encoder.apply(utils.network_utils.init_weights)

    # Set up solver
    generator_solver     = None
    image_encoder_solver = None
    if cfg.TRAIN.POLICY == 'adam':
        generator_solver     = torch.optim.Adam(generator.parameters(), lr=cfg.TRAIN.GENERATOR_LEARNING_RATE, betas=cfg.TRAIN.BETAS)
        image_encoder_solver = torch.optim.Adam(filter(lambda p: p.requires_grad, image_encoder.parameters()), lr=cfg.TRAIN.IMAGE_ENCODER_LEARNING_RATE, betas=cfg.TRAIN.BETAS)
    elif cfg.TRAIN.POLICY == 'sgd':
        generator_solver     = torch.optim.SGD(generator.parameters(), lr=cfg.TRAIN.GENERATOR_LEARNING_RATE, momentum=cfg.TRAIN.MOMENTUM)
        image_encoder_solver = torch.optim.SGD(filter(lambda p: p.requires_grad, image_encoder.parameters()), lr=cfg.TRAIN.IMAGE_ENCODER_LEARNING_RATE, betas=cfg.TRAIN.BETAS)
    else:
        raise Exception('[FATAL] %s Unknown optimizer %s.' % (dt.now(), cfg.TRAIN.POLICY))

    # Set up learning rate scheduler to decay learning rates dynamically
    generator_lr_scheduler     = torch.optim.lr_scheduler.MultiStepLR(generator_solver, milestones=cfg.TRAIN.GENERATOR_LR_MILESTONES, gamma=0.1)
    image_encoder_lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(image_encoder_solver, milestones=cfg.TRAIN.IMAGE_ENCODER_LR_MILESTONES, gamma=0.1)

    if torch.cuda.is_available():
        generator.cuda()
        image_encoder.cuda()

    # Set up loss functions
    bce_loss = torch.nn.BCELoss()

    # Load pretrained model if exists
    init_epoch     = 0
    best_iou       = -1
    best_epoch     = -1
    if 'WEIGHTS' in cfg.CONST and cfg.TRAIN.RESUME_TRAIN:
        print('[INFO] %s Recovering from %s ...' % (dt.now(), cfg.CONST.WEIGHTS))
        checkpoint = torch.load(cfg.CONST.WEIGHTS)
        init_epoch = checkpoint['epoch_idx']
        best_iou   = checkpoint['best_iou']
        best_epoch = checkpoint['best_epoch']

        generator.load_state_dict(checkpoint['generator_state_dict'])
        generator_solver.load_state_dict(checkpoint['generator_solver_state_dict'])
        image_encoder.load_state_dict(checkpoint['image_encoder_state_dict'])
        image_encoder_solver.load_state_dict(checkpoint['image_encoder_solver_state_dict'])

        print('[INFO] %s Recover complete. Current epoch #%d, Best IoU = %.4f at epoch #%d.' \
                 % (dt.now(), init_epoch, best_iou, best_epoch))

    # Training loop
    for epoch_idx in range(init_epoch, cfg.TRAIN.NUM_EPOCHES):
        n_batches = len(train_data_loader)
        # Average meterics
        epoch_image_encoder_loss    = []
        epoch_generator_loss        = []
        
        # Tick / tock
        epoch_start_time = time()

        for batch_idx, (taxonomy_names, sample_names, rendering_images, voxels) in enumerate(train_data_loader):
            n_samples = len(voxels)
            if not n_samples == cfg.CONST.BATCH_SIZE:
                continue

            # Tick / tock
            batch_start_time = time()

            # switch models to training mode
            generator.train();
            image_encoder.train();

            # Get data from data loader
            rendering_images = utils.network_utils.var_or_cuda(rendering_images)
            voxels           = utils.network_utils.var_or_cuda(voxels)

            # Train the generator and the image encoder
            rendering_image_features    = image_encoder(rendering_images)
            generated_voxels            = generator(rendering_image_features)
            image_encoder_loss          = bce_loss(generated_voxels, voxels) * 10

            generator.zero_grad()
            image_encoder.zero_grad()
            
            image_encoder_loss.backward()
            
            generator_solver.step()
            image_encoder_solver.step()

            # Tick / tock
            batch_end_time = time()
            
            # Append loss and accuracy to average metrics
            epoch_image_encoder_loss.append(image_encoder_loss.item())
            # Append loss and accuracy to TensorBoard
            n_itr = epoch_idx * n_batches + batch_idx
            train_writer.add_scalar('Generator/ImageEncoderLoss', image_encoder_loss.item(), n_itr)
            # Append rendering images of voxels to TensorBoard
            if n_itr % cfg.TRAIN.VISUALIZATION_FREQ == 0:
                # TODO: add GT here ...
                gv           = generated_voxels.cpu().data[:8].numpy()
                voxel_views  = utils.binvox_visualization.get_voxel_views(gv, os.path.join(img_dir, 'train'), n_itr)
                train_writer.add_image('Reconstructed Voxels', voxel_views, n_itr)

            print('[INFO] %s [Epoch %d/%d][Batch %d/%d] Total Time = %.3f (s) ILoss = %.4f' % \
                (dt.now(), epoch_idx + 1, cfg.TRAIN.NUM_EPOCHES, batch_idx + 1, n_batches, batch_end_time - batch_start_time, image_encoder_loss))

        # Tick / tock
        image_encoder_mean_loss = np.mean(epoch_image_encoder_loss)
        train_writer.add_scalar('Generator/MeanLoss', image_encoder_mean_loss, epoch_idx + 1)
        epoch_end_time = time()
        print('[INFO] %s Epoch [%d/%d] Total Time = %.3f (s) ILoss = %.4f' % 
            (dt.now(), epoch_idx + 1, cfg.TRAIN.NUM_EPOCHES, epoch_end_time - epoch_start_time, image_encoder_mean_loss))

        # Validate the training models
        iou = test_net(cfg, epoch_idx + 1, output_dir, val_data_loader, val_writer, generator, image_encoder)

        # Save weights to file
        if (epoch_idx + 1) % cfg.TRAIN.SAVE_FREQ == 0:
            if not os.path.exists(ckpt_dir):
                os.makedirs(ckpt_dir)
            utils.network_utils.save_checkpoints(os.path.join(ckpt_dir, 'ckpt-epoch-%04d.pth.tar' % (epoch_idx + 1)), \
                    epoch_idx + 1, generator, generator_solver, image_encoder, image_encoder_solver, best_iou, best_epoch)
        elif iou > best_iou:
            if not os.path.exists(ckpt_dir):
                os.makedirs(ckpt_dir)
            
            best_iou   = iou
            best_epoch = epoch_idx + 1
            utils.network_utils.save_checkpoints(os.path.join(ckpt_dir, 'best-ckpt.pth.tar'), \
                    epoch_idx + 1, generator, generator_solver, image_encoder, image_encoder_solver, best_iou, best_epoch)

    # Close SummaryWriter for TensorBoard
    train_writer.close()
    val_writer.close()

