from __future__ import print_function
import argparse
import os
import subprocess
import torch

import torch.nn as nn
import torch.optim as optim
import torchvision.models as models

from torchvision import datasets, transforms
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel


def dist_init(host_addr, rank, local_rank, world_size, port=23456):
    host_addr_full = 'tcp://' + host_addr + ':' + str(port)
    torch.distributed.init_process_group(backend='nccl', init_method=host_addr_full,
                                         rank=rank, world_size=world_size)
    assert torch.distributed.is_initialized()
    torch.cuda.init()
    torch.cuda.set_device(local_rank)
    
def cleanup():
    torch.distributed.destroy_process_group()

class ResNet18(nn.Module):
    def __init__(self):
        super(ResNet18, self).__init__()
        self.model = models.resnet18(weights=None, num_classes=10)
        self.model.conv1 = nn.Conv2d(1, 64, kernel_size=7, stride=2, padding=3, bias=False)

    def forward(self, x):
        return self.model(x)

def train(args, model, local_rank, train_loader, criterion, optimizer, epoch, rank):
    model.train()
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(local_rank), target.to(local_rank)
        optimizer.zero_grad()
        output = model(data)
        loss = criterion(output, target)
        loss.backward()
        optimizer.step()
        if batch_idx % args.log_interval == 0 and rank == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(data), len(train_loader.dataset),
                100. * batch_idx / len(train_loader), loss.item()))

def test(args, model, local_rank, test_loader, criterion, world_size, rank):
    model.eval()
    test_loss = 0
    correct = 0
    length = len(test_loader.dataset)/world_size
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(local_rank), target.to(local_rank)
            output = model(data)
            test_loss += criterion(output, target).item() # sum up batch loss
            pred = output.argmax(dim=1, keepdim=True) # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= length
    if rank == 0:
        print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
            test_loss, correct, length,
            100. * correct / length))

def main():

    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch MNIST Example')
    parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                        help='input batch size for testing (default: 1000)')
    parser.add_argument('--epochs', type=int, default=10, metavar='N',
                        help='number of epochs to train (default: 10)')
    parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                        help='learning rate (default: 0.01)')
    parser.add_argument('--momentum', type=float, default=0.5, metavar='M',
                        help='SGD momentum (default: 0.5)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--save-model', action='store_true', default=False,
                        help='For Saving the current Model')
    parser.add_argument('--datasetDir',
                        help='Please add your dataset directory')

    args = parser.parse_args()

    # Initialize process group
    rank = int(os.environ['SLURM_PROCID'])
    local_rank = int(os.environ['SLURM_LOCALID'])
    world_size = int(os.environ['SLURM_NTASKS'])
    iplist = os.environ['SLURM_JOB_NODELIST']
    ip = subprocess.getoutput(f"scontrol show hostname {iplist} | head -n1")

    dist_init(ip, rank, local_rank, world_size)

    # Load MNIST dataset
    train_dataset = datasets.MNIST(args.datasetDir, train=True, download=False,
                       transform=transforms.Compose([
                           transforms.ToTensor(),
                           transforms.Normalize((0.1307,), (0.3081,))
                       ]))
    train_sampler = DistributedSampler(train_dataset, num_replicas=world_size, rank=rank)
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        sampler=train_sampler
        )
    test_dataset = datasets.MNIST(args.datasetDir, train=False, transform=transforms.Compose([
                           transforms.ToTensor(),
                           transforms.Normalize((0.1307,), (0.3081,))
                       ]))
    test_sampler = DistributedSampler(test_dataset, num_replicas=world_size, rank=rank)
    test_loader = DataLoader(
        test_dataset,
        batch_size=args.test_batch_size,
        sampler=test_sampler
        )

    init_start_event = torch.cuda.Event(enable_timing=True)
    init_end_event = torch.cuda.Event(enable_timing=True)

    # Create model and optimizer
    model = ResNet18().to(local_rank)
    model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
    model = DistributedDataParallel(model)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum)

    # Train and test the model
    init_start_event.record()
    for epoch in range(1, args.epochs + 1):
        train(args, model, local_rank, train_loader, criterion, optimizer, epoch, rank)
        test(args, model, local_rank, test_loader, criterion, world_size, rank)
    init_end_event.record()

    if rank == 0:
        print(f"CUDA event elapsed time: {init_start_event.elapsed_time(init_end_event) / 1000}sec")
        # print(f"{model}")

    if (args.save_model):
        torch.save(model.state_dict(),"mnist_cnn.pt")

    cleanup()

if __name__ == "__main__":
    main()
