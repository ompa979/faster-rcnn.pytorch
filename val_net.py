
# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiasen Lu, Jianwei Yang, based on code from Ross Girshick
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import os
import sys
import numpy as np
import argparse
import pprint
import pdb
import time
import cv2
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import pickle
from roi_data_layer.roidb import combined_roidb
from roi_data_layer.roibatchLoader import roibatchLoader
from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.rpn.bbox_transform import clip_boxes
from model.nms.nms_wrapper import nms
from model.rpn.bbox_transform import bbox_transform_inv
from model.utils.net_utils import save_net, load_net, vis_detections
from model.faster_rcnn.vgg16 import vgg16
from model.faster_rcnn.resnet import resnet
from sorting_models import sort_models

import pdb

try:
    xrange          # Python 2
except NameError:
    xrange = range  # Python 3


def parse_args():
  """
  Parse input arguments
  """
  parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')
  parser.add_argument('--dataset', dest='dataset',
                      help='training dataset',
                      default='pascal_voc', type=str)
  parser.add_argument('--cfg', dest='cfg_file',
                      help='optional config file',
                      default='cfgs/vgg16.yml', type=str)
  parser.add_argument('--net', dest='net',
                      help='vgg16, res50, res101, res152',
                      default='res101', type=str)
  parser.add_argument('--set', dest='set_cfgs',
                      help='set config keys', default=None,
                      nargs=argparse.REMAINDER)
  parser.add_argument('--load_dir', dest='load_dir',
                      help='directory to load models', default="/work/acarbo/faster_rcnn/data/underwood2_models",
                      type=str)
  parser.add_argument('--cuda', dest='cuda',
                      help='whether use CUDA',
                      action='store_true')
  parser.add_argument('--ls', dest='large_scale',
                      help='whether use large imag scale',
                      action='store_true')
  parser.add_argument('--mGPUs', dest='mGPUs',
                      help='whether use multiple GPUs',
                      action='store_true')
  parser.add_argument('--cag', dest='class_agnostic',
                      help='whether perform class_agnostic bbox regression',
                      action='store_true')
  parser.add_argument('--parallel_type', dest='parallel_type',
                      help='which part of model to parallel, 0: all, 1: model before roi pooling',
                      default=0, type=int)
  parser.add_argument('--checksession', dest='checksession',
                      help='checksession to load model',
                      default=1, type=int)
  parser.add_argument('--checkepoch', dest='checkepoch',
                      help='checkepoch to load network',
                      default=1, type=int)
  parser.add_argument('--checkpoint', dest='checkpoint',
                      help='checkpoint to load network',
                      default=10021, type=int)
  parser.add_argument('--bs', dest='batch_size',
                      help='batch_size',
                      default=1, type=int)
  parser.add_argument('--vis', dest='vis',
                      help='visualization mode',
                      action='store_true')
  args = parser.parse_args()
  return args

lr = cfg.TRAIN.LEARNING_RATE
momentum = cfg.TRAIN.MOMENTUM
weight_decay = cfg.TRAIN.WEIGHT_DECAY

if __name__ == '__main__':

  args = parse_args()

  print('Called with args:')
  print(args)

  if torch.cuda.is_available() and not args.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")

  np.random.seed(cfg.RNG_SEED)
  if args.dataset == "pascal_voc":
      args.imdb_name = "voc_2007_trainval"
      args.imdbval_name = "voc_2007_test"
      args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
  elif args.dataset == "pascal_voc_0712":
      args.imdb_name = "voc_2007_trainval+voc_2012_trainval"
      args.imdbval_name = "voc_2007_test"
      args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
  elif args.dataset == "underwood":
      args.imdb_name = "train"
      args.imdbval_name = "val"
      args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]', 'MAX_NUM_GT_BOXES', '50']
  elif args.dataset == "coco":
      args.imdb_name = "coco_2014_train+coco_2014_valminusminival"
      args.imdbval_name = "coco_2014_minival"
      args.set_cfgs = ['ANCHOR_SCALES', '[4, 8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
  elif args.dataset == "imagenet":
      args.imdb_name = "imagenet_train"
      args.imdbval_name = "imagenet_val"
      args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
  elif args.dataset == "vg":
      args.imdb_name = "vg_150-50-50_minitrain"
      args.imdbval_name = "vg_150-50-50_minival"
      args.set_cfgs = ['ANCHOR_SCALES', '[4, 8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']

  args.cfg_file = "cfgs/{}_ls.yml".format(args.net) if args.large_scale else "cfgs/{}.yml".format(args.net)

  if args.cfg_file is not None:
    cfg_from_file(args.cfg_file)
  if args.set_cfgs is not None:
    cfg_from_list(args.set_cfgs)

  print('Using config:')
  pprint.pprint(cfg)

  cfg.TRAIN.USE_FLIPPED = False
  imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbval_name)
  #imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbval_name)


  #imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbval_name)
  #imdb.competition_mode(on=True)

  print('{:d} roidb entries'.format(len(roidb)))
  print(args.dataset)
  input_dir = args.load_dir + "/" + args.net + "/" + args.dataset
  if not os.path.exists(input_dir):
    raise Exception('There is no input directory for loading network from ' + input_dir)
  load_name = os.path.join(input_dir,
    'faster_rcnn_{}_{}_{}.pth'.format(args.checksession, args.checkepoch, args.checkpoint))

  # initilize the network here.
  if args.net == 'vgg16':
    fasterRCNN = vgg16(imdb.classes, pretrained=False, class_agnostic=args.class_agnostic)
  elif args.net == 'res101':
    fasterRCNN = resnet(imdb.classes, 101, pretrained=False, class_agnostic=args.class_agnostic)
  elif args.net == 'res50':
    fasterRCNN = resnet(imdb.classes, 50, pretrained=False, class_agnostic=args.class_agnostic)
  elif args.net == 'res152':
    fasterRCNN = resnet(imdb.classes, 152, pretrained=False, class_agnostic=args.class_agnostic)
  else:
    print("network is not defined")
    pdb.set_trace()

  fasterRCNN.create_architecture()

  files = sort_models("/work/acarbo/faster_rcnn/data/underwood2_models/vgg16/underwood/")
  print("Number of epochs",len(files))
  loss_final_save = torch.zeros(len(files))
  epoch = 0
  start = time.time()
  for file in files:
    path_to_file = input_dir + "/" + file
    print("load checkpoint %s" % (path_to_file))
    checkpoint = torch.load(path_to_file)
    fasterRCNN.load_state_dict(checkpoint['model'])
    if 'pooling_mode' in checkpoint.keys():
        cfg.POOLING_MODE = checkpoint['pooling_mode']


    print('load model successfully!')
  # initilize the tensor holder here.
    im_data = torch.FloatTensor(1)
    im_info = torch.FloatTensor(1)
    num_boxes = torch.LongTensor(1)
    gt_boxes = torch.FloatTensor(1)

    # ship to cuda
    if args.cuda:
        im_data = im_data.cuda()
        im_info = im_info.cuda()
        num_boxes = num_boxes.cuda()
        gt_boxes = gt_boxes.cuda()

    # make variable
    im_data = Variable(im_data, volatile=True)
    im_info = Variable(im_info, volatile=True)
    num_boxes = Variable(num_boxes, volatile=True)
    gt_boxes = Variable(gt_boxes, volatile=True)

    if args.cuda:
        cfg.CUDA = True

    if args.cuda:
        fasterRCNN.cuda()

    
    max_per_image = 100

    vis = args.vis

    if vis:
        thresh = 0.05
    else:
        thresh = 0.0

    save_name = 'faster_rcnn_10'

    #Modificacio meva training estava a false
    dataset = roibatchLoader(roidb, ratio_list, ratio_index, args.batch_size, \
                            imdb.num_classes, training=True, normalize = False)
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=args.batch_size,
                                shuffle=False, num_workers=0,
                                pin_memory=True)
    #data_iter = iter(dataloader)
    num_images = len(dataset)
    all_boxes = [[[] for _ in xrange(num_images)]
                for _ in xrange(imdb.num_classes)]
    output_dir = get_output_dir(imdb, save_name)
    
    _t = {'im_detect': time.time(), 'misc': time.time()}
    det_file = os.path.join(output_dir, 'detections.pkl')

    fasterRCNN.train()
    empty_array = np.transpose(np.array([[],[],[],[],[]]), (1,0))

    loss_temp = 0
    
    
    for i, data in enumerate(dataloader):
    
      im_data.data.resize_(data[0].size()).copy_(data[0])
      im_info.data.resize_(data[1].size()).copy_(data[1])
      gt_boxes.data.resize_(data[2].size()).copy_(data[2])
      num_boxes.data.resize_(data[3].size()).copy_(data[3])
      #det_tic = time.time()
      rois, cls_prob, bbox_pred, \
      rpn_loss_cls, rpn_loss_box, \
      RCNN_loss_cls, RCNN_loss_bbox, \
      rois_label = fasterRCNN(im_data, im_info, gt_boxes, num_boxes)

      
      loss = rpn_loss_cls.mean() + rpn_loss_box.mean() \
           + RCNN_loss_cls.mean() + RCNN_loss_bbox.mean()                   

      loss_temp += loss.data[0]      
      
      sys.stdout.write('im_detect: {:d}/{:d} in epoch {:d} \r' \
          .format(i + 1, num_images, epoch+1))
      sys.stdout.flush()
      
      if vis:
          cv2.imwrite('result.png', im2show)
          pdb.set_trace()
          #cv2.imshow('test', im2show)
          #cv2.waitKey(0)

    
    loss_final_save[epoch] = loss_temp/num_images
    print('Loss a epoch {:d}: {:.3f}'.format(epoch+1,loss_final_save[epoch]))
    print(loss_final_save[epoch])
    print(loss_final_save)   
    epoch = epoch + 1
  end = time.time()
  print("test time: %0.4fs" % (end - start))
if not os.path.exists('output_train'):
  os.makedirs('output_train')
pickle.dump(loss_final_save,open('output_train_nou/val_loss.pkl','wb'))
