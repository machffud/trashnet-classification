# -*- coding: utf-8 -*-
"""trashnet-training-modeling.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1y_BHvMEh_nJ1fYgbXQiUE_Pjm4JTICwT

# Takehome test trashnet classification

by Machffud Tra H. V (machffud.tra@ui.ac.id)
"""

# from google.colab import drive
# drive.mount('/content/drive')

!pip install datasets

"""## Setup & Helpers"""

!pip install -Uq fastai einops ml_collections

!nvidia-smi

import os
import glob
import functools

from ml_collections import config_dict

import numpy as np
from einops.layers.torch import Rearrange

from PIL import Image
from matplotlib import pyplot as plt

from fastai.vision.all import *
from torchvision.transforms import RandAugment

from sklearn.metrics import classification_report, accuracy_score

"""**IMPORTANT**: Adjust training configuration and seed below:"""

cfg = config_dict.ConfigDict()
cfg.use_augmentations = True
cfg.do_export_clf_results = True
cfg.bs = 64
cfg.data_path = "data"

# To ensure reproducibility
# Huge variance can occur (between seeds) due to
# random validation splits and the stochastic nature of the training
set_seed(18264)

# Analysis Helpers
def assess_test_performance(learner, tdl, vocab):
    preds = learner.get_preds(dl=tdl, with_decoded=True)
    print(classification_report(*preds[1:], digits=4, target_names=vocab))

def assess_test_performance_with_tta(learner, tdl, vocab):
    preds = learner.tta(dl=tdl)
    print(classification_report(preds[1], preds[0].argmax(1), digits=4, target_names=vocab))

def get_validation_performance(learner, dls):
    """Returns accuracy for valid set"""
    preds = learner.get_preds(dl=dls.valid, with_decoded=True)
    return accuracy_score(*preds[1:])

class RandAugmentTransform(RandTransform):
    "A fastai transform handler/wrapper for RandAugment (https://arxiv.org/abs/1909.13719)"
    split_idx, order = None, 2
    def __init__(self): store_attr()

    def before_call(self, b, split_idx):
        self.idx = split_idx
        self.aug = RandAugment()

    def encodes(self, img: PILImage):
        return self.aug(img) if self.idx == 0 else img

"""## Data"""

# %cd drive/MyDrive
# !ls trashnetDataset
# #!cp -r trashnetDataset/data adamata
# !ls adamata
# %cd adamata

import os
import shutil
from datasets import load_dataset
from sklearn.model_selection import train_test_split

# Download the dataset
dataset = load_dataset('garythung/trashnet')

def add_image_name(dataset):
    label_counts = {}

    def generate_image_name(example):
        label = example['label']
        if label not in label_counts:
            label_counts[label] = 0
        image_name = f"{label}_{label_counts[label]}"
        label_counts[label] += 1
        example['image_name'] = image_name
        return example
    dataset = dataset.map(generate_image_name)
    return dataset
dataset["train"] = add_image_name(dataset["train"])
# Create directories for train and test datasets
os.makedirs('data/train', exist_ok=True)
os.makedirs('data/test', exist_ok=True)

# Get the dataset in dictionary format

dataset_dict = dataset['train'].train_test_split(test_size=0.2, stratify_by_column='label')

"""Prepare data files (rename and splitting). Stratified splitting to train and test with ratio 4:1."""

# Function to save the split dataset to disk
def save_dataset(split_dataset, split_name):
    for item in split_dataset:
        label = item['label']
        img = item['image']

        # Create directories for each label
        label_dir = os.path.join('data', split_name, str(label))
        os.makedirs(label_dir, exist_ok=True)

        # Save image
        img_path = os.path.join(label_dir, f"{item['image_name']}.jpg")
        img.save(img_path)

# Save the train and test datasets
save_dataset(dataset_dict['train'], 'train')
save_dataset(dataset_dict['test'], 'test')

print("Dataset has been split and saved successfully.")

!find data -maxdepth 2 -type d -ls

"""Set up transforms and augmentations. As default, we will use Imagenet mean/std.dev (arbitrary choice, we can also use `[0.5, 0.5, 0.5]`) to normalize our images. To enhance robustness and prevent overfitting, we use the following augmentations:
1. Random Resize Then Crop - preserving the image size 224x224 and to capture smaller or sections of the image
2. [RandAugment](https://arxiv.org/abs/1909.13719) - SOTA set of augmentation (pretty standard in SOTA models in addition to CutMix and MixUp)

We do not use CutMix or MixUp to keep the model simple and the training fast (since getting the absolute best model performance is not the main objective here).
"""

item_tfms = [RandomResizedCrop(224, min_scale=0.3), RandAugmentTransform()] if cfg.use_augmentations else []
batch_tfms = [Normalize.from_stats(*imagenet_stats)] # Just normalize to imagenet

"""Set up dataloaders for training, validation, and testing. For validation, we split it (20%) from the initial training set."""

# Training and Validation Dataloaders
dls = ImageDataLoaders.from_folder(cfg.data_path, bs=cfg.bs, valid_pct=0.2, item_tfms=item_tfms, batch_tfms=batch_tfms)
dls.show_batch() # should show the effect of RandAugment

tdl = dls.test_dl(get_image_files(Path(cfg.data_path)/"test"), with_labels=True)
tdl.show_batch() # should not show RandAugment

"""## Modeling

We will create and evaluate Standard ResNet34


To downsample the image 4x, standard ResNet would utilize a 7x7 convolution layer with stride and padding combined with a max pool layer.
```python
# Standard
      self.conv1 = nn.Conv2d(3, self.inplanes, kernel_size=7, stride=2, padding=3, bias=False)
      self.bn1 = nn.BatchNorm2d(self.inplanes)
      self.gelu = nn.ReLU(inplace=True)
      self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

```


To train the models, we utilize the `AdamW` (default optimizer provided by fast.ai) with decoupled weight decay and the following parameters:
- `mom`: $β_{1}=0.9$
- `sqr_mom`: $β_{2}=0.99$
- `eps`: $ϵ=10^{-5}$
- `wd`: $λ=0.01$

For the initial learning rate, we use the LR finder algorithm described by Leslie Smith (https://arxiv.org/abs/1506.01186) to determine a good starting learning rate. For the training process themselves, we use a 3-stage training process with first, a linear training with constant learning rate (as determined by the algorithm) for about 24 epochs then continuing to a second stage with one-cycle scheduling for 24 epochs. To check (and hopefully ensures) convergence, we will 'train' the model for another 12 epochs. To ensure we get the best results, we will use `SaveModelCallback` provided by fastai to load the best model in the end.

As per usual with image classification, we use cross entropy loss as our loss function (specifically `CrossEntropyLossFlat` provided by fastai that is the same as `torch.nn.CrossEntropyLoss` except for automatically flattening inputs and targets). The inputs themselves are 3 channels 224x224 images (3x224x224) with batch size of 64 (64x3x224x224).

To ensure correctness, we will **train the models 5 times using different seeds**. Hence, this code/notebook below are meant to be demonstrative and the results demonstrated are not meant to be taken directly (since they are just 1 of 3 final results).
"""

def start_train(learner, epochs=[24, 24, 12], tdl=tdl):
    def get_lr_():
        lr = learner.lr_find()
        plt.show()
        print("Fit LR:", lr)
        return lr

    name = type(learner.model).__name__
    dls = learner.dls

    # start normal fit
    learner.fit(epochs[0], get_lr_())

    # start fit_one_cycle
    learner.fit_one_cycle(epochs[1], get_lr_())

    # convergence fit
    learner.fit_one_cycle(epochs[2], get_lr_())

    learner.save(name)
    print(f"Training done for {name}")

"""### ResNet Block

To start with, we use the following definition for a (basic) ResNet block. The only notable difference would be the change from ReLU to GELU to better conform with modern training practices.
"""

# Adapted from https://pytorch.org/vision/stable/_modules/torchvision/models/resnet.html
class BasicResNetBlock(nn.Module):
    """Basic ResNet Block (no bottleneck) with GELU instead of RELU"""
    expansion: int = 1

    def __init__(
        self,
        inplanes: int,
        planes: int,
        stride: int = 1,
        downsample = None
    ) -> None:
        super().__init__()
        # Both self.conv1 and self.downsample layers downsample the input when stride != 1
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=3, padding=1, stride=stride, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.gelu = nn.GELU()
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, padding=1, stride=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x: Tensor) -> Tensor:
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.gelu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.gelu(out)

        return out

"""### Conventional ResNet-34

The code for ResNet34 below was directly taken from a commonly used implementation (`torchvision`) to ensure validity and adapted to remove extraneous bits unused.
"""

# Adapted from https://pytorch.org/vision/stable/_modules/torchvision/models/resnet.html
class ResNet(nn.Module):
    def __init__(
        self,
        block,
        layers,
        num_classes = 6,
        zero_init_residual = True,
    ) -> None:
        super().__init__()

        self.inplanes = 64

        self.conv1 = nn.Conv2d(3, self.inplanes, kernel_size=7, stride=2, padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(self.inplanes)
        self.gelu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2)
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out")
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

        # Zero-initialize the last BN in each residual branch,
        # so that the residual branch starts with zeros, and each residual block behaves like an identity.
        # This improves the model by 0.2~0.3% according to https://arxiv.org/abs/1706.02677
        if zero_init_residual:
            for m in self.modules():
                if isinstance(m, BasicResNetBlock):
                    nn.init.constant_(m.bn2.weight, 0)

    def _make_layer(
        self,
        block,
        planes: int,
        blocks: int,
        stride: int = 1,
    ) -> nn.Sequential:
        downsample = None
        if stride != 1 or self.inplanes != planes:
        downsample = nn.Sequential(
            nn.Conv2d(self.inplanes, planes, 1, stride, bias=False),
            nn.BatchNorm2d(planes),
        )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))

        self.inplanes = planes

        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x: Tensor) -> Tensor:
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.gelu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        x = self.avgpool(x)
        x = torch.flatten(x, 1)
        x = self.fc(x)

        return x

resnet34 = ResNet(BasicResNetBlock, [3, 4, 6, 3])
r34_cbs = [EarlyStoppingCallback(monitor="f1_score", min_delta=0.001, patience=4), SaveModelCallback(monitor="f1_score", fname="r34", reset_on_fit=False)]
learner_resnet34 = Learner(dls, resnet34, metrics=[accuracy, F1Score(average="macro")], cbs=r34_cbs)

"""As can be seen from the model summary below, the inputs to the ResNet blocks are downsampled 4x from 224x224 to 56x56 by the `stem` (`Conv2d(7, stride=2, padding=3)` and `MaxPool2d(3, stride=2, padding=1)`)."""

learner_resnet34.summary()

start_train(learner_resnet34)

assess_test_performance(learner_resnet34, tdl, dls.vocab)
assess_test_performance_with_tta(learner_resnet34, tdl, dls.vocab)

"""## Analysis"""

!mkdir -p results

def evaluate(learner, dl=tdl, vocab=dls.vocab):
    name = type(learner.model).__name__
    interp = ClassificationInterpretation.from_learner(learner, dl=tdl)
    preds = learner.get_preds(dl=dl, with_decoded=True)

    print(classification_report(*preds[1:], digits=4, target_names=vocab))
    interp.plot_confusion_matrix(figsize=(10, 10))
    interp.plot_top_losses(k=25, figsize=(15, 15))

    # generate and output classification report

    report = classification_report(*preds[1:], digits=6, output_dict=True, target_names=vocab)
    report_df = pd.DataFrame(report).transpose()
    report_df.to_csv(Path("./results")/f"{name}_report.csv")

    # save model predictions
    vs = [(str(path), int(gt), int(pred)) for path, gt, pred in zip(tdl.items, *preds[1:])]
    pred_df = pd.DataFrame(vs, columns=["path", "gt", "pred"])
    pred_df.to_csv(Path("./results")/f"{name}_preds.csv")

"""### Evaluation"""

evaluate(learner_resnet34)