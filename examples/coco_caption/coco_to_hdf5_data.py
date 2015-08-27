#!/usr/bin/env python

from hashlib import sha1
import os
import random
random.seed(3)
import re
import sys
import h5py
import numpy as np
import pickle as pkl

sys.path.append('.')
sys.path.append('../../data/coco/coco')

COCO_PATH = '../../data/coco/coco'
COCO_TOOL_PATH = '%s/PythonAPI/build/lib/pycocotools' % COCO_PATH
IM_FOLDER = 'images2'
COCO_IM_FOLDER = 'images'
COCO_IMAGE_ROOT = '%s/%s' % (COCO_PATH, IM_FOLDER)
FEATURE_ROOT = '/y/lisaanne/image_captioning/coco_features'
home_dir = '/home/lisaanne/caffe-LSTM'

MAX_HASH = 100000

sys.path.append(COCO_TOOL_PATH)
from pycocotools.coco import COCO

from hdf5_sequence_generator import SequenceGenerator, HDF5SequenceWriter

# UNK_IDENTIFIER is the word used to identify unknown words
UNK_IDENTIFIER = '<unk>'

SENTENCE_SPLIT_REGEX = re.compile(r'(\W+)')
def split_sentence(sentence):
  # break sentence into a list of words and punctuation
  sentence = [s.lower() for s in SENTENCE_SPLIT_REGEX.split(sentence.strip()) if len(s.strip()) > 0]
  # remove the '.' from the end of the sentence
  if sentence[-1] != '.':
    # print "Warning: sentence doesn't end with '.'; ends with: %s" % sentence[-1]
    return sentence
  return sentence[:-1]

MAX_WORDS = 20

class CocoSequenceGenerator(SequenceGenerator):
  def __init__(self, coco, batch_num_streams, image_root, vocab=None,
               max_words=MAX_WORDS, align=True, shuffle=True, gt_captions=True,
               pad=True, truncate=True, split_ids=None, feats_bool=False):
    self.max_words = max_words
    self.feats_bool = feats_bool
    num_empty_lines = 0
    self.images = []
    num_total = 0
    num_missing = 0
    num_captions = 0
    known_images = {}
    self.coco = coco
    if split_ids is None:
      split_ids = coco.imgs.keys()
    self.image_path_to_id = {}
    for image_id in split_ids:
      image_info = coco.imgs[image_id]
      image_path = '%s/%s' % (image_root, image_info['file_name'])
      self.image_path_to_id[image_path] = image_id
      if os.path.isfile(image_path):
        assert image_id not in known_images  # no duplicates allowed
        known_images[image_id] = {}
        known_images[image_id]['path'] = image_path
        if gt_captions:
          known_images[image_id]['sentences'] = [split_sentence(anno['caption'])
              for anno in coco.imgToAnns[image_id]]
          num_captions += len(known_images[image_id]['sentences'])
        else:
          known_images[image_id]['sentences'] = []
      else:
        num_missing += 1
        print 'Warning (#%d): image not found: %s' % (num_missing, image_path)
      num_total += 1
    print '%d/%d images missing' % (num_missing, num_total)
    if vocab is None:
      self.init_vocabulary(known_images)
    else:
      self.vocabulary_inverted = vocab
      self.vocabulary = {}
      for index, word in enumerate(self.vocabulary_inverted):
        self.vocabulary[word] = index
    self.image_sentence_pairs = []
    num_no_sentences = 0
    for image_filename, metadata in known_images.iteritems():
      if not metadata['sentences']:
        num_no_sentences += 1
        print 'Warning (#%d): image with no sentences: %s' % (num_no_sentences, image_filename)
      for sentence in metadata['sentences']:
        self.image_sentence_pairs.append((metadata['path'], sentence))
    self.index = 0
    self.num_resets = 0
    self.num_truncates = 0
    self.num_pads = 0
    self.num_outs = 0
    self.image_list = []
    SequenceGenerator.__init__(self)
    self.batch_num_streams = batch_num_streams
    # make the number of image/sentence pairs a multiple of the buffer size
    # so each timestep of each batch is useful and we can align the images
    if align:
      num_pairs = len(self.image_sentence_pairs)
      remainder = num_pairs % batch_num_streams
      if remainder > 0:
        num_needed = batch_num_streams - remainder
        for i in range(num_needed):
          choice = random.randint(0, num_pairs - 1)
          self.image_sentence_pairs.append(self.image_sentence_pairs[choice])
      assert len(self.image_sentence_pairs) % batch_num_streams == 0
    if shuffle:
      random.shuffle(self.image_sentence_pairs)
    self.pad = pad
    self.truncate = truncate
    self.negative_one_padded_streams = frozenset(('input_sentence', 'target_sentence'))

  def streams_exhausted(self):
    return self.num_resets > 0

  def init_vocabulary(self, image_annotations, min_count=5):
    words_to_count = {}
    for image_id, annotations in image_annotations.iteritems():
      for annotation in annotations['sentences']:
        for word in annotation:
          word = word.strip()
          if word not in words_to_count:
            words_to_count[word] = 0
          words_to_count[word] += 1
    # Sort words by count, then alphabetically
    words_by_count = sorted(words_to_count.keys(), key=lambda w: (-words_to_count[w], w))
    print 'Initialized vocabulary with %d words; top 10 words:' % len(words_by_count)
    for word in words_by_count[:10]:
      print '\t%s (%d)' % (word, words_to_count[word])
    # Add words to vocabulary
    self.vocabulary = {UNK_IDENTIFIER: 0}
    self.vocabulary_inverted = [UNK_IDENTIFIER]
    for index, word in enumerate(words_by_count):
      word = word.strip()
      if words_to_count[word] < min_count:
        break
      self.vocabulary_inverted.append(word)
      self.vocabulary[word] = index + 1
    print 'Final vocabulary (restricted to words with counts of %d+) has %d words' % \
        (min_count, len(self.vocabulary))

  def dump_vocabulary(self, vocab_filename):
    print 'Dumping vocabulary to file: %s' % vocab_filename
    with open(vocab_filename, 'wb') as vocab_file:
      for word in self.vocabulary_inverted:
        vocab_file.write('%s\n' % word)
    print 'Done.'

  def dump_image_file(self, image_filename, dummy_image_filename=None):
    print 'Dumping image list to file: %s' % image_filename
    with open(image_filename, 'wb') as image_file:
      for image_path, _ in self.image_list:
        image_file.write('%s\n' % image_path)
    if dummy_image_filename is not None:
      print 'Dumping image list with dummy labels to file: %s' % dummy_image_filename
      with open(dummy_image_filename, 'wb') as image_file:
        for path_and_hash in self.image_list:
          image_file.write('%s %d\n' % path_and_hash)
    print 'Done.'

  def next_line(self):
    num_lines = float(len(self.image_sentence_pairs))
    self.index += 1
    if self.index == 1 or self.index == num_lines or self.index % 10000 == 0:
      print 'Processed %d/%d (%f%%) lines' % (self.index, num_lines,
                                              100 * self.index / num_lines)
    if self.index == num_lines:
      self.index = 0
      self.num_resets += 1

  def line_to_stream(self, sentence):
    stream = []
    for word in sentence:
      word = word.strip()
      if word in self.vocabulary:
        stream.append(self.vocabulary[word])
      else:  # unknown word; append UNK
        stream.append(self.vocabulary[UNK_IDENTIFIER])
    # increment the stream -- 0 will be the EOS character
    stream = [s + 1 for s in stream]
    return stream

  def get_pad_value(self, stream_name):
    return -1 if stream_name in self.negative_one_padded_streams else 0

  def get_streams(self):
    image_filename, line = self.image_sentence_pairs[self.index]
    stream = self.line_to_stream(line)
    pad = self.max_words - (len(stream) + 1) if self.pad else 0
    if pad > 0: self.num_pads += 1
    self.num_outs += 1
    out = {}
    out['stage_indicators'] = [1] * (len(stream) + 1) + [0] * pad
    out['cont_sentence'] = [0] + [1] * len(stream) + [0] * pad
    out['input_sentence'] = [0] + stream + [-1] * pad
    out['target_sentence'] = stream + [0] + [-1] * pad
    truncated = False
    if self.truncate:
      for key, val in out.iteritems():
        if len(val) > self.max_words:
          out[key] = val[:self.max_words]
          truncated = True
      self.num_truncates += truncated
    image_hash = self.image_hash(image_filename)
    out['hashed_image_path'] = [image_hash] * len(out['input_sentence'])
    self.image_list.append((image_filename, image_hash))
    self.next_line()
    return out

  def image_hash(self, filename):
    image_hash = int(sha1(filename).hexdigest(), 16) % MAX_HASH
    assert image_hash == float(image_hash)
    return image_hash

COCO_ANNO_PATH = '%s/annotations/captions_%%s2014.json' % COCO_PATH
COCO_IMAGE_PATTERN = '%s/%s/%%s2014' % (COCO_PATH, COCO_IM_FOLDER)
COCO_IMAGE_ID_PATTERN = 'COCO_%s2014_%%012d.jpg'

BUFFER_SIZE = 100
OUTPUT_DIR = 'h5_data/buffer_%d' % BUFFER_SIZE
SPLITS_PATTERN = '/home/lisaanne/caffe-LSTM/data/coco/coco2014_cocoid.%s.txt'
OUTPUT_DIR_PATTERN = '%s/%%s_batches' % OUTPUT_DIR

def process_dataset(split_name, coco_split_name, batch_stream_length,
                    vocab=None, aligned=True, vocab_tag=''):
  with open(SPLITS_PATTERN % split_name, 'r') as split_file:
    split_image_ids = [int(line) for line in split_file.readlines()]
  output_dataset_name = split_name
  if aligned:
    output_dataset_name += '_aligned_%d' % MAX_WORDS
  else:
    output_dataset_name += '_unaligned'
  output_path = OUTPUT_DIR_PATTERN % output_dataset_name
  #coco (I think) just puts annotations together for train/test set.  Want to use trainval set for my own images (I think... need to ask Jeff)
  coco = COCO(COCO_ANNO_PATH % split_name)  #THIS IS CHANGED!!!  This means that you have to have a json file with the captions for your split!
  image_root = COCO_IMAGE_PATTERN % coco_split_name
  sg = CocoSequenceGenerator(coco, BUFFER_SIZE, image_root,
      split_ids=split_image_ids, vocab=vocab, align=aligned, pad=aligned,
      truncate=aligned)
  if vocab is None:
    vocab_out_path = '%s/%svocabulary.txt' % (OUTPUT_DIR, vocab_tag)
    sg.dump_vocabulary(vocab_out_path)
  sg.batch_stream_length = batch_stream_length
  writer = HDF5SequenceWriter(sg, output_dir=output_path)
  writer.write_to_exhaustion(min_sent_length=2)
  writer.write_filelists()
  image_out_path = '%s/image_list.txt' % output_path
  image_dummy_labels_out_path = '%s/image_list.with_dummy_labels.txt' % output_path
  sg.dump_image_file(image_out_path, image_dummy_labels_out_path)
  num_outs = sg.num_outs
  num_pads = sg.num_pads
  num_truncates = sg.num_truncates
  print 'Padded %d/%d sequences; truncated %d/%d sequences' % \
      (num_pads, num_outs, num_truncates, num_outs)
  return sg.vocabulary_inverted

def output_train_sentences(split_name, coco_split_name, batch_stream_length, file_save,
                    vocab=None, aligned=True):
  with open(SPLITS_PATTERN % split_name, 'r') as split_file:
    split_image_ids = [int(line) for line in split_file.readlines()]
  output_dataset_name = split_name
  if aligned:
    output_dataset_name += '_aligned_%d' % MAX_WORDS
  else:
    output_dataset_name += '_unaligned'
  output_path = OUTPUT_DIR_PATTERN % output_dataset_name
  coco = COCO(COCO_ANNO_PATH % coco_split_name)
  image_root = COCO_IMAGE_PATTERN % coco_split_name
  sg = CocoSequenceGenerator(coco, BUFFER_SIZE, image_root,
      split_ids=split_image_ids, vocab=vocab, align=aligned, pad=aligned,
      truncate=aligned)
  
  v = sg.vocabulary.keys()
  sentences = sg.image_sentence_pairs

  txt = open(file_save,'wb')

  for ii, sent in enumerate(sentences):
    print 'On %d of %d.\n' %(ii, len(sentences))
    s = sent[1]
    final_s = ''
    total_w = len(s)
    for i, word in enumerate(s):
      if word in v:
        final_s += word
      else:
        final_s += '<unk>'
      if i == total_w-1:
        final_s += '.\n'
      else:
        final_s += ' '
    print final_s + '\n'
    txt.write(final_s)
  txt.close()        
  return sg.vocabulary_inverted

def write_im_hdf5(im_list, save_name):
  ims_per_file = 20000
  h5_list = '%s_h5_list.txt' %save_name
  h5_file = open(h5_list, 'w')
  for ix, im in enumerate(im_list):
    if ix % 100 == 0:
      print '%s: on %d of %d.\n' %(save_name, ix, len(im_list))
    full_im = '%s/%s_vgg_fc7Feat.p' %(FEATURE_ROOT, im.split('/')[-1].split('.')[0])
    feat = pkl.load(open(full_im, 'rb'))
    if ix % ims_per_file == 0:
      if ix > 0:
        save_name_full = '%s/%s_%d.h5' %(home_dir, save_name, ix/ims_per_file)
        f = h5py.File(save_name_full)
        labels = np.ones((min(len(im_list), ims_per_file)))
        f.create_dataset('label', data=labels)
        f.create_dataset('data', data=full_feat)
        f.close()
        h5_file.writelines(('%s\n' %save_name_full))
      full_feat = np.zeros((min(len(im_list), ims_per_file), len(feat['fc7'])))
      count_feats = 0
    full_feat[count_feats,:] = feat['fc7']
    count_feats += 1
  if not (ix-1) % ims_per_file == 0: # need to write remainder of items
    write_feats = full_feat[:count_feats,:]
    save_name_full = '%s_%d.h5' %(save_name, (ix/ims_per_file)+1)
    f = h5py.File(save_name_full)
    f.create_dataset('label', data=np.ones((min(len(im_list), ims_per_file))))
    f.create_dataset('data', data=full_feat)
    f.close()
    h5_file.writelines(('%s\n.' %save_name_full))

  h5_file.close() 

def process_coco(tag='', include_val = True, include_trainval=False):
  vocab = None
  datasets = [
      (tag+'train', 'trainval', 100000, True)]
  if include_val:
      datasets += [
      (tag+'val', 'val', 100000, True),
      (tag+'val_train', 'val', 100000, True),
      (tag+'val_novel', 'val', 100000, True),
      (tag+'test', 'test', 100000, True)]
      # Write unaligned datasets as well:
#      ('train', 'train', 100000, False),
#      ('val', 'val', 100000, False),
#      ('test', 'val', 100000, False),
  # Also create a 'trainval' set if include_trainval is set.
  # ./data/coco/make_trainval.py must have been run for this to work.
  if include_trainval:
    datasets += [
      (tag+'trainval', 'trainval', 100000, True),
#      ('trainval', 'trainval', 100000, False),
    ]
  for split_name, coco_split_name, batch_stream_length, aligned in datasets:
    vocab = process_dataset(split_name, coco_split_name, batch_stream_length,
                            vocab=vocab, vocab_tag=tag, aligned=aligned)
  pkl.dump(vocab, open(('vocab_dicts/%s_vocab.p' %tag),'wb'))  

def add_dataset(tag, split):
  vocab = pkl.load(open(('vocab_dicts/%s_vocab.p' %tag), 'rb'))
  split_name = tag + split
  coco_split_name = 'trainval' 
  batch_stream_length = 100000
  aligned = True
  vocab = process_dataset(split_name, coco_split_name, batch_stream_length,
                          vocab=vocab, aligned=aligned)

if __name__ == "__main__":
  process_coco('augment_train_noShortEOS_', False, False)
  #process_coco('only_noun_sentences_noZebra', False, False)
#  tag = 'captions_augment_train_set_NN300_noZebra_train' 
#  add_dataset(tag, 'vocab_dicts/captions_augment_train_set_NN300_noZebra_train_vocab.p')
  
  #make new train/test splits
#  identifiers = ['fixVocab.fixFlag.black_bike.blue_train.red_car.yellow_shirt.green_car.train', 'fixVocab.fixFlag.black_bike.blue_train.red_car.yellow_shirt.green_car.val', 'fixVocab.fixFlag.black_bike.blue_train.red_car.yellow_shirt.green_car.val_novel', 'fixVocab.fixFlag.black_bike.blue_train.red_car.yellow_shirt.green_car.val_train']
#  vocab_tag = 'fixVocab.fixFlag.black_bike.blue_train.red_car.yellow_shirt.green_car'
#  vocab = None
#  for identifier in identifiers:
#    split_name = identifier 
#    coco_split_name = 'trainval'
#    batch_stream_length = 100000
#    aligned = True
#    vocab = process_dataset(split_name, coco_split_name, batch_stream_length,
#                             vocab=vocab, aligned=aligned, vocab_tag=vocab_tag)
#
#   #just need to read images and then put into an hdf5 file
#    output_dataset_name = split_name + '_aligned_20'
#    output_path = OUTPUT_DIR_PATTERN % output_dataset_name
#    image_out_path = '%s/image_list.txt' % output_path
#  
#    im_file = open(image_out_path, 'rb') 
#    im_list = im_file.readlines()
#
#    save_name = '%s/%s' %(output_path, identifier)
#    write_im_hdf5(im_list, save_name)

#  vocab = None
#  datasets = [
#      ('train', 'train', 100000, True, 'train_sentences.txt'),
#      ('val', 'val', 100000, True, 'val_sentences.txt'),
#      ('trainval', 'trainval', 100000, True, 'trainval_sentences.txt')]
#  for split_name, coco_split_name, batch_stream_length, aligned, file_save in datasets:
#    vocab = output_train_sentences(split_name, coco_split_name, batch_stream_length, file_save, vocab=None, aligned=True)
