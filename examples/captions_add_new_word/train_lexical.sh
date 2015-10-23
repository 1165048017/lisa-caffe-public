#!/usr/bin/env bash

DATA_DIR=../../examples/coco_caption/h5_data/

GPU_ID=2
WEIGHTS=\
../../models/bvlc_reference_caffenet/bvlc_reference_caffenet.caffemodel
#../../models/vgg/VGG_ILSVRC_16_layers.caffemodel
#/x/lisaanne/coco_attribute/train_lexical_classifier/attributes_JJ100_NN300_VB100_imagenet_zebra.vgg_iter_20000.solverstate

export PYTHONPATH=.

../../build/tools/caffe train \
    -solver solver_lexical.prototxt \
    -weights $WEIGHTS \
    -gpu $GPU_ID

