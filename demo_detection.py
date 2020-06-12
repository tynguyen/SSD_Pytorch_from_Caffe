from __future__ import division

from models.prototxt_to_pytorch import CaffeNet
from utils.logger import *
from utils.utils import *
from utils.datasets import *
from utils.parse_config import *
from utils.train_saver import Saver
from utils.lr_scheduler import LR_Scheduler
#from test import evaluate

from terminaltables import AsciiTable

import os
import sys
import time
import datetime
import argparse

import torch
from torch.utils.data import DataLoader
from torchvision import datasets
from torchvision import transforms
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
import torch.optim as optim
import pdb 
import matplotlib.pyplot as plt
import tensorflow as tf
import logging 

DEPLOY_MODEL_DEF  =os.environ['DEPLOY_MODEL_DEF']
DATA_CONFIG  = os.environ['DATA_CONFIG']
DATASET      = os.environ['DATASET']
LOG_DIR_ROOT = os.environ['LOGDIR']
CROP_IMG_SIZE= os.environ['CROP_IMG_SIZE']
PRETRAINED_CKPT= None #os.environ['PRETRAINED_CKPT']

class Tester(object):
    def __init__(self, **kwargs):
        # Fix random seed
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        opt = kwargs.get("args")
        self.opt = opt 
        self.batch_size  = opt.batch_size
        self.img_size    = opt.img_size
        self.saver       = Saver(opt)
        
        # Save parameters
        self.saver.save_experiment_config({'args': opt})
        print("====================================================")
        print(">> Save params, weights to %s"%self.saver.experiment_dir)

        self.tboard_summary = PytorchLogger(opt)
        print(">> Save tboard logs to %s"%self.tboard_summary.log_dir)

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # Get data configuration
        data_config = parse_data_config(opt.data_config)
        test_path = opt.test_img_dir
        class_names = load_classes(data_config["names"])
        self.num_classes = len(class_names)
        # Colors for tensorboard visualization    
        self.tf_bbox_colors = np.random.randn(self.num_classes,3)
        
        # Get folder dataloader
        test_dataset = ImageFolder(test_path, img_size=opt.img_size, square_make_type=opt.square_make_type)


        self.test_dataloader = torch.utils.data.DataLoader(
            test_dataset,
            batch_size=opt.batch_size,
            shuffle=False,
            num_workers=opt.n_cpu,
            pin_memory=True,
        )
   
        # Define model 
        self.model = CaffeNet(args.caffe_model_def).to(self.device) 
        self.model.apply(weights_init_normal)
        #print(self.model)

        #TODO: load the pretrained ckpt
        pretrained_weights = self.opt.resume
        if not pretrained_weights:
            pretrained_weights = self.opt.pretrained_weights 
        if pretrained_weights:
            ckpt = torch.load(pretrained_weights)
            try:
                if 'state_dict' in ckpt.keys():
                    self.model.load_state_dict(ckpt['state_dict'])
                    print("=> Loaded ckpt %s using ckpt['state_dict']!"%(pretrained_weights))            
                else:
                    self.model.load_state_dict(ckpt)
                    print("=> Loaded ckpt %s using ckpt!"%(pretrained_weights))            

            except:
                print("=> Loaded ckpt %s using ckpt['state_dict']/ckpt does not work!"%(pretrained_weights))            
                pass

            try:
                self.best_loss = ckpt['best_loss']
                self.start_epoch = ckpt['epoch']
                print("=> Loaded ckpt %s, epoch %d, loss = %.4f"%(pretrained_weights, self.start_epoch, self.best_loss))            
            except:
                print("=> Loaded ckpt %s"%(pretrained_weights))            

        # Set output
        self.model.set_verbose(False) # Turn of forward ... printing 
        self.model.set_eval_outputs('detection_out')
        self.model.set_forward_net_only(True) # Ignore Data, Annotated Data layer if exist
        
    def testing(self):
        self.model.eval()
        num_batches = len(self.test_dataloader)
        for batch_i, (_, imgs) in enumerate(self.test_dataloader):
            imgs    = Variable(imgs.to(self.device), requires_grad=False)

            # Turn on and off detection_out since it's very slow
            self.model.set_detection_output(True) 
            self.model.set_eval_outputs('detection_out')

            with torch.no_grad():
                blobs   = self.model(imgs)

            str2Print="- Batch [%d/%d]|"%(batch_i, num_batches)
            print(str2Print)
            logging.info(str2Print)
            

            # Visualize image & gt bboxes
            # Get BBox errors
            #blobs: Tupble with 'mbox_loss','mbox_conf', 'mbox_loc', 'detection_out' tensors
            if isinstance(blobs, (list, tuple)):
                detections = blobs[-1]      
            else:
                detections = blobs      
            
            assert detections.shape[1] == 7, "Detection out must be Nx7. This one is Nx%d"%detections.shape[1]
            print(">> Number boxes per image: %.2f"%(detections.size(0)/float(self.batch_size)))
            neg_mask1 = detections[:, 3:] < 0.
            neg_mask2 = detections[:, 3:] > 1.0 
            detections[:, 3:][neg_mask1] = 0.001 
            detections[:, 3:][neg_mask2] = 0.99 
            #detections = torch.masked_select(detections, mask1)
            #detections = torch.masked_select(detections, mask2)
            self.visualize_batch(batch_i, imgs, detections, self.tboard_summary, msg='test/pred_batch', max_box_per_class=3)

        print("===============================")

    
    def visualize_batch(self, step, imgs, targets, tboard_summary, msg='train/gt', max_imgs=2, max_box_per_class=10):
        '''Visualize ground truth bboxes
        Inputs:
            targets N x 6 
        '''    
        if targets.size(1) == 7:
            targets = torch.cat((targets[:,:2], targets[:,3:]) ,1)
        assert targets.size(1) == 6, "Targets must have dimension of Nx6. This targets size Nx%d"%targets.size(1)

        batch_size = imgs.shape[0]
        start_t  = time.time() 
        max_imgs = min(batch_size, max_imgs) 
        list_draw_imgs = []
        for i in range(max_imgs):
            box_idx = targets[:,0] == i
            labels  = targets[box_idx, 1].detach().cpu().numpy()
            boxes   = targets[box_idx, 2:]
            
            # if max_box_per_class: (boxes are sorted)
            if max_box_per_class:
                box_idx = [] 
                for cl in range(1, self.num_classes):
                    cl_idx = np.where(labels == cl)[0] 
                    count  = min(len(cl_idx), max_box_per_class)
                    # shuffer
                    #sub_cl_idx = np.random.choice(cl_idx, count) 
                    sub_cl_idx = cl_idx[:count]
                    if cl == 1:
                        box_idx = sub_cl_idx.flatten()
                    else:
                        box_idx = np.concatenate([box_idx, sub_cl_idx.flatten()]).flatten()
                labels = labels[box_idx]
                boxes  = boxes[box_idx]

            # Convert boxes to y1x1y2x2 format
            boxes   = xyxy2yxyx(boxes)
            if len(boxes.shape) == 1:
                boxes = boxes.view(1,1,-1)
            elif len(boxes.shape) == 2:
                boxes = boxes.unsqueeze(0)
            np_boxes = boxes.detach().cpu().numpy() 
            img = imgs[i].transpose(0,1).transpose(1,2)
            img = img.unsqueeze(0)
            np_img = img.detach().cpu().numpy() 
            
                    
            colors = np.empty([np_boxes.shape[1], 3])
            for j in range(np_boxes.shape[1]):
                colors[j] = self.tf_bbox_colors[int(labels[j])]
            tf_img = tf.image.draw_bounding_boxes(np_img, np_boxes, colors)
            #with  tboard_summary.as_default():
                #tf.summary.image(msg, tf_img, step)

            draw_img = tf_img.numpy() 
            draw_img = draw_img[0].transpose([2, 0, 1])
            list_draw_imgs.append(draw_img[np.newaxis,...])
        
        stacked_draw_imgs = np.concatenate(list_draw_imgs, 0)
        tboard_summary.add_images(msg, stacked_draw_imgs, step) 
        #print(">> VISUAL TIME: ", time.time() - start_t)
    
        
        
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0, help="Random seed")
    parser.add_argument("--GPUs", type=int, default=0, help="GPU ID")
    parser.add_argument("--lr", type=float, default=1e-3, help="learning rate")
    parser.add_argument('--lr_scheduler', type=str, default='step', choices=['poly', 'step', 'cos'])
    parser.add_argument("--dataset", type=str, default=DATASET, help="Name of the dataset")
    parser.add_argument("--batch_size", type=int, default=32, help="size of each image batch")
    parser.add_argument("--caffe_model_def", type=str, default=DEPLOY_MODEL_DEF, help="path to caffe model definition file (prototxt)")
    parser.add_argument("--data_config", type=str, default=DATA_CONFIG, help="path to data config file")
    parser.add_argument("--test_img_dir", type=str, default='', help="path to test images")
    parser.add_argument("--checkpoint_dir", type=str, default=LOG_DIR_ROOT + '/checkpoints', help="path to data config file")
    parser.add_argument("--log_dir", type=str, default=LOG_DIR_ROOT + '/logs', help="path to logdir")
    parser.add_argument("--checkname", type=str, default='', help="Subdir of checkpoints")
    parser.add_argument("--resume", type=str, default='', help="Checkpoints to resume")
    parser.add_argument("--pretrained_weights", type=str, help="if specified starts from checkpoint model", default=PRETRAINED_CKPT)
    parser.add_argument("--n_cpu", type=int, default=8, help="number of cpu threads to use during batch generation")
    parser.add_argument("--img_size", type=int, default=CROP_IMG_SIZE, help="size of each image dimension")
    parser.add_argument("--evaluation_interval", type=int, default=1, help="interval evaluations on validation set")
    parser.add_argument("--write_image_interval", type=int, default=500, help="interval writing images to tensorboard")
    parser.add_argument("--compute_map", default=False, help="if True computes mAP every tenth batch")
    parser.add_argument("--square_make_type", default='crop', help="How to make the input image have square shape", choices=['crop', 'pad'])
    parser.add_argument("--conf_thres", default=0.5, help="conf threshold", type=float)
    parser.add_argument("--nms_thres", default=0.5, help="nms threshold", type=float)
    parser.add_argument("--visdom", default='visdom', help="Use visdom to visualize or not", type=str)

    args = parser.parse_args()
    print(args)
    tester = Tester(args=args)

    if torch.cuda.is_available():
        if args.GPUs:
            torch.set_default_tensor_type('torch.cuda.FloatTensor')
        if not args.GPUs:
            print("WARNING: It looks like you have a CUDA device, but aren't " +
		  "using CUDA.\nRun with --cuda for optimal training speed.")
            torch.set_default_tensor_type('torch.FloatTensor')
    else:
        torch.set_default_tensor_type('torch.FloatTensor')
    
    if args.GPUs:
        cudnn.benchmark = True 
   
    # Start training 
    tester.testing()
    
    
    
