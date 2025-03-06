

import builtins
import torch.distributed as dist
import os
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import datetime
import time
import numpy as np
import math
from torch.utils.data import DataLoader
import model.ResNet as models
from model.CaCo import CaCo, CaCo_PN
from ops.os_operation import mkdir, mkdir_rank
from training.train_utils import adjust_learning_rate2,save_checkpoint,adjust_learning_rate
from data_processing.loader import TwoCropsTransform, TwoCropsTransform2,GaussianBlur,Solarize
from ops.knn_monitor import knn_monitor
import torch.optim as optim
from torchvision.datasets import CIFAR10, STL10,Imagenette 

class LARS2(torch.optim.Optimizer):
    """
    LARS optimizer with explicit exclusion of SplitBatchNorm parameters and biases
    from weight decay and LARS adaptation.
    """
    def __init__(self, params, lr=0, weight_decay=0, momentum=0.9, trust_coefficient=0.001):
        defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum, 
                        trust_coefficient=trust_coefficient)
        super().__init__(params, defaults)

    def _is_splitbatchnorm(self, p):
        """Check if the parameter belongs to a SplitBatchNorm layer"""
        if hasattr(p, '_module_name'):
            module_name = p._module_name.lower()
            return 'splitbatchnorm' in module_name or 'splitbn' in module_name
        return False
    
    def _is_bias(self, p):
        """Check if the parameter is a bias"""
        if hasattr(p, '_param_name'):
            return 'bias' in p._param_name.lower()
        return False

    @torch.no_grad()
    def step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            weight_decay = group['weight_decay']
            momentum = group['momentum']
            trust_coefficient = group['trust_coefficient']
            lr = group['lr']

            for p in group['params']:
                if p.grad is None:
                    continue
                
                d_p = p.grad
                
                # Skip weight decay for SplitBatchNorm and bias parameters
                if weight_decay != 0 and not (self._is_splitbatchnorm(p) or self._is_bias(p)):
                    d_p = d_p.add(p, alpha=weight_decay)
                
                # Apply LARS adaptation only for non-SplitBatchNorm and non-bias parameters
                if not (self._is_splitbatchnorm(p) or self._is_bias(p)):
                    param_norm = torch.norm(p)
                    update_norm = torch.norm(d_p)
                    
                    if param_norm != 0 and update_norm != 0:
                        # LARS coefficient
                        lars_coef = trust_coefficient * param_norm / update_norm
                        d_p = d_p.mul(lars_coef)
                
                # Apply momentum and update
                param_state = self.state[p]
                if 'momentum_buffer' not in param_state:
                    param_state['momentum_buffer'] = torch.zeros_like(p)
                
                buf = param_state['momentum_buffer']
                buf.mul_(momentum).add_(d_p)
                
                p.add_(buf, alpha=-lr)
        
        return loss

# Helper function to set parameter attributes for better tracking
def set_module_name_to_params(model):
    for name, module in model.named_modules():
        for param_name, param in module.named_parameters(recurse=False):
            param._module_name = name
            param._param_name = param_name
            
# Example of how to set up parameter groups with your ResNet model
def setup_optimizer_with_no_lr_scheduler_for_projection_head(model, base_lr=1.0, 
                                                           weight_decay=1e-6, 
                                                           momentum=0.9,
                                                           trust_coefficient=0.001):
    # Tag parameters with their module names for better identification
    set_module_name_to_params(model)
    
    # Separate parameters into projection head and the rest of the model
    projection_head_params = []
    other_params = []
    
    for name, param in model.named_parameters():
        if name.startswith('fc.'):
            projection_head_params.append(param)
        else:
            other_params.append(param)
    
    # Create parameter groups
    param_groups = [
        {'params': other_params, 'lr': base_lr, 'weight_decay': weight_decay},
        {'params': projection_head_params, 'lr': base_lr, 'weight_decay': weight_decay}
    ]
    
    # Create the optimizer with these groups
    optimizer = LARS2(param_groups, lr=base_lr, weight_decay=weight_decay, 
                     momentum=momentum, trust_coefficient=trust_coefficient)
    
    # Create your LR scheduler, but only apply it to the non-projection head parameters
    def lr_lambda(epoch, total_epochs=800):  # Default total_epochs=100
        # Define your learning rate schedule logic here
        # For example, a cosine decay schedule:
        return 0.5 * (1 + math.cos(math.pi * epoch /total_epochs))
    
    # This scheduler will only adjust the learning rate for the first parameter group (non-projection head)
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer, 
        lr_lambda=[lambda epoch: lr_lambda(epoch), lambda _: 1.0]
    )
    
    return optimizer, scheduler

def init_log_path(args,batch_size):
    """
    :param args:
    :return:
    save model+log path
    """
    save_path = os.path.join(os.getcwd(), args.log_path)
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, args.dataset)
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "Type_"+str(args.type))
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "lr_" + str(args.lr) + "_" + str(args.lr_final))
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "memlr_"+str(args.memory_lr) +"_"+ str(args.memory_lr_final))
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "t_" + str(args.moco_t) + "_memt" + str(args.mem_t))
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "wd_" + str(args.weight_decay) + "_memwd" + str(args.mem_wd)) 
    mkdir_rank(save_path,args.rank)
    if args.moco_m_decay:
        save_path = os.path.join(save_path, "mocomdecay_" + str(args.moco_m))
    else:
        save_path = os.path.join(save_path, "mocom_" + str(args.moco_m))
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "memgradm_" + str(args.mem_momentum))
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "hidden" + str(args.mlp_dim)+"_out"+str(args.moco_dim))
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "batch_" + str(batch_size))
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "epoch_" + str(args.epochs))
    mkdir_rank(save_path,args.rank)
    save_path = os.path.join(save_path, "warm_" + str(args.warmup_epochs))
    mkdir_rank(save_path,args.rank)
    return save_path

def main_worker(args):
    params = vars(args)
    print(vars(args))
    init_lr = args.lr * args.batch_size / 256
    total_batch_size = args.batch_size
    print("init lr",init_lr," init batch size",args.batch_size)
    # create model
    print("=> creating model '{}'".format(args.arch))

    Memory_Bank = CaCo_PN(args.cluster,args.moco_dim)

    model = CaCo(models.__dict__[args.arch], args,
                           args.moco_dim, args.moco_m)
    print(model.encoder_q)
    optimizer, scheduler = setup_optimizer_with_no_lr_scheduler_for_projection_head(model)

    
    #optimizer = torch.optim.SGD(model.parameters(), init_lr,
                                #momentum=args.momentum,
                                #weight_decay=args.weight_decay)
 
    from model.optimizer import  AdamW
    from model.optimizer import  LARS
    #optimizer = AdamW(model.parameters(), init_lr, betas=(0.9, 0.999), eps=1e-8, weight_decay=args.weight_decay)
    #optimizer = LARS(model.parameters(), args.lr ,weight_decay=args.weight_decay,momentum=args.momentum)
    
    #optimizer = torch.optim.SGD(model.parameters(), args.lr, args.weight_decay, args.momentum)


    model.cuda()
    Memory_Bank.cuda()
    print("per gpu batch size: ",args.batch_size)
    print("current workers:",args.workers)
    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()

    save_path = init_log_path(args,total_batch_size)
    if not args.resume:
        args.resume = os.path.join(save_path,"checkpoint_best.pth.tar")
        print("searching resume files ",args.resume)
    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            if args.gpu is None:
                checkpoint = torch.load(args.resume)
            else:
                # Map model to be loaded to specified single gpu.
                loc = 'cuda:{}'.format(args.gpu)
                checkpoint = torch.load(args.resume, map_location=loc,weights_only=False)
            args.start_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            Memory_Bank.load_state_dict(checkpoint['Memory_Bank'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    # Data loading code
    if args.dataset=='stl10':
        #traindir = os.path.join(args.data, 'train')
        normalize = transforms.Normalize(mean=[0.4914, 0.4822, 0.4465],
                                     std=[0.2023, 0.1994, 0.2010])
        if args.multi_crop:
            from data_processing.MultiCrop_Transform import Multi_Transform
            multi_transform = Multi_Transform([32, 24],
                                              [2, 2],
                                              [1.0, 0.5],
                                              [1.0, 1.0], normalize)
            train_dataset = datasets.ImageFolder(
                traindir, multi_transform)
        else:

            augmentation1 = transforms.Compose([
                    transforms.RandomResizedCrop(32),
                    transforms.RandomApply([
                        transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)  # not strengthened
                    ], p=0.8),
                    transforms.RandomGrayscale(p=0.2),
                    #transforms.RandomApply([GaussianBlur([.1, 2.])], p=1.0),
                    
                    transforms.RandomHorizontalFlip(p=0.5),
                    transforms.ToTensor(),
                    normalize
                ])

            augmentation2 = transforms.Compose([
                    transforms.RandomResizedCrop(32),
                    transforms.RandomApply([
                        transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)  # not strengthened
                    ], p=0.8),
                    transforms.RandomGrayscale(p=0.2),
                    #transforms.RandomApply([GaussianBlur([.1, 2.])], p=0.1),
                    #transforms.RandomApply([Solarize()], p=0.1),
                    transforms.RandomHorizontalFlip(p=0.5),
                    transforms.ToTensor(),
                    normalize
                ])
            
    
                                    
            train_dataset = CIFAR10(root='./datasets', train=True, download=True, transform=TwoCropsTransform(augmentation1))
            #train_dataset = CIFAR10(root='./datasets', train=True, download=True, transform=transform)
            #train_dataset = STL10(root='./data', split='unlabeled', download=True, transform=TwoCropsTransform2(augmentation1, augmentation2))
            #train_dataset = Imagenette(root =  './data', split= 'train', size= 'full', download=True, transform =TwoCropsTransform2(augmentation1, augmentation2))
            
        testdir = os.path.join(args.data, 'val')
        transform_test = transforms.Compose([
            
            #transforms.Resize(32),
            #transforms.CenterCrop(224),
            transforms.ToTensor(),
            normalize,
        ])
        from data_processing.imagenet import imagenet
        val_dataset =CIFAR10(root='./datasets', train=True, download=True, transform=transform_test)
        #val_dataset = STL10(root='./data', split='train', download=True, transform=transform_test)
        #val_dataset= Imagenette(root =  './data/val', split= 'train', size= 'full', download=True, transform =transform_test)
        
        test_dataset =CIFAR10(root='./datasets', train=False, download=True, transform=transform_test)
        #test_dataset = STL10(root='./data', split='test', download=True, transform=transform_test
        #test_dataset = Imagenette(root =  './data/test', split= 'val', size= 'full', download=True, transform =transform_test)

    else:
        print("We only support ImageNet dataset currently")
        exit()
    train_loader = DataLoader(train_dataset, shuffle=True, batch_size=args.batch_size,pin_memory=True,num_workers=args.workers,drop_last=True)
    val_loader = DataLoader(val_dataset, shuffle=False, batch_size=args.knn_batch_size,pin_memory=True,num_workers=args.workers,drop_last=False)
    test_loader = DataLoader(test_dataset, shuffle=False, batch_size=args.knn_batch_size,pin_memory=True,num_workers=args.workers,drop_last=False)

    #init weight for memory bank
    bank_size=args.cluster
    print("finished the data loader config!")
    model.eval()
    print("gpu consuming before running:", torch.cuda.memory_allocated()/1024/1024)
    #init memory bank
    if args.ad_init and not os.path.isfile(args.resume):
        from training.init_memory import init_memory
        init_memory(train_loader, model, Memory_Bank, criterion,
              optimizer, 0, args)
        print("Init memory bank finished!!")
    knn_path = os.path.join(save_path,"knn.log")
    train_log_path = os.path.join(save_path,"train.log")
    best_Acc=0
    for epoch in range(args.start_epoch, args.epochs):

        #adjust_learning_rate(optimizer, epoch, args)
        #adjust_learning_rate2(optimizer, epoch, args, args.lr)
        scheduler.step()
        #if args.type<10:
        if args.moco_m_decay:
            moco_momentum = adjust_moco_momentum(epoch, args)
        else:
            moco_momentum = args.moco_m
        print("current moco momentum %f"%moco_momentum)
        # train for one epoch
        
        from training.train_caco import train_caco
        acc1 = train_caco(train_loader, model, Memory_Bank, criterion,
                                optimizer, epoch, args, train_log_path,moco_momentum)

        if epoch%args.knn_freq==0 or epoch<=20 or epoch==621:
            print("gpu consuming before cleaning:", torch.cuda.memory_allocated()/1024/1024)
            torch.cuda.empty_cache()
            print("gpu consuming after cleaning:", torch.cuda.memory_allocated()/1024/1024)
            knn_test_acc=knn_monitor(model.encoder_q, val_loader, test_loader,epoch, args,global_k = args.knn_neighbor) 
            print({'*KNN monitor Accuracy': knn_test_acc})
            if args.rank ==0:
                    with open(knn_path,'a+') as file:
                        file.write('%d epoch KNN monitor Accuracy %f\n'%(epoch,knn_test_acc))
            
                                         
                        #global_k=min(args.knn_neighbor,len(val_loader.dataset))
            
            #except:
                #print("small error raised in knn calcu")
                #knn_test_acc=0

            torch.cuda.empty_cache()
            epoch_limit=20
            if knn_test_acc<=1.0 and epoch>=epoch_limit:
                exit()
        is_best=best_Acc>acc1
        best_Acc=max(best_Acc,acc1)

        save_dict={
            'epoch': epoch + 1,
            'arch': args.arch,
            'best_acc':best_Acc,
            'knn_acc': knn_test_acc,
            'state_dict': model.state_dict(),
            'optimizer': optimizer.state_dict(),
            'Memory_Bank':Memory_Bank.state_dict(),
            }


        if epoch%10==9:
            tmp_save_path = os.path.join(save_path, 'checkpoint_{:04d}.pth.tar'.format(epoch))
            torch.save(model, os.path.join(save_path, 'my_model.pth'))
            save_checkpoint(save_dict, is_best=False, filename=tmp_save_path)
        tmp_save_path = os.path.join(save_path, 'checkpoint_best.pth.tar')

        save_checkpoint(save_dict, is_best=is_best, filename=tmp_save_path)
def adjust_moco_momentum(epoch, args):
    """Adjust moco momentum based on current epoch"""
    return 1. - 0.5 * (1. + math.cos(math.pi * epoch / args.epochs)) * (1. - args.moco_m)
