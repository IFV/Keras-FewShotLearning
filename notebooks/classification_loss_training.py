#%%
import shutil
from datetime import datetime
from pathlib import Path

import imgaug.augmenters as iaa
import numpy as np
import pandas as pd
import tensorflow as tf
import yaml
from tensorflow.python.keras.models import Model
from tensorflow.python.keras import applications as keras_applications
from tensorflow.python.keras.callbacks import (
    ModelCheckpoint,
    ReduceLROnPlateau,
    TensorBoard
)
from tensorflow.python.keras.layers import Input
from tensorflow.python.keras.optimizer_v2.adam import Adam

from keras_fsl.models import SiameseNets
from keras_fsl.sequences import training, prediction
from keras_fsl.losses import ClassificationLoss

#%% Init data
output_folder = Path('logs') / 'classification_loss' / datetime.today().strftime('%Y%m%d-%H%M%S')
output_folder.mkdir(parents=True, exist_ok=True)
try:
    shutil.copy(__file__, output_folder / 'training_pipeline.py')
except (FileNotFoundError, NameError):
    pass

all_annotations = (
    pd.read_csv('data/annotations/cropped_images.csv')
    .assign(
        day=lambda df: df.image_name.str.slice(3, 11),
        image_name=lambda df: 'data/images/cropped_images/' + df.image_name,
    )
)
train_val_test_split = yaml.safe_load(open('data/annotations/cropped_images_split.yaml'))
train_set = all_annotations.loc[lambda df: df.day.isin(train_val_test_split['train_set_dates'])]
val_set = all_annotations.loc[lambda df: df.day.isin(train_val_test_split['val_set_dates'])]
test_set = all_annotations.loc[lambda df: df.day.isin(train_val_test_split['test_set_dates'])].reset_index(drop=True)

#%% Init model
branch_model_name = 'MobileNet'

preprocessing = iaa.Sequential([
    iaa.Fliplr(0.5),
    iaa.Flipud(0.5),
    iaa.Affine(rotate=(-180, 180)),
    iaa.CropToFixedSize(224, 224, position='center'),
    iaa.PadToFixedSize(224, 224, position='center'),
    iaa.AssertShape((None, 224, 224, 3)),
    iaa.Lambda(lambda images_list, *_: (
        getattr(keras_applications, branch_model_name.lower())
        .preprocess_input(np.stack(images_list), data_format='channels_last')
    )),
])

siamese_nets = SiameseNets(
    branch_model={
        'name': branch_model_name,
        'init': {'include_top': False, 'input_shape': (224, 224, 3), 'pooling': 'avg'}
    },
    head_model={
        'name': 'MixedNorms',
        'init': {
            'norms': [
                lambda x: 1 - tf.nn.l2_normalize(x[0]) * tf.nn.l2_normalize(x[1]),
                lambda x: tf.math.abs(x[0] - x[1]),
                lambda x: tf.nn.softmax(tf.math.abs(x[0] - x[1])),
                lambda x: tf.square(x[0] - x[1]),
            ]
        }
    }
)
branch_depth = len(siamese_nets.get_layer('branch_model').layers)

#%% Train model with product loss
# tf.config.experimental_run_functions_eagerly(True)
batch_size = 64
labels = Input((1, ), batch_size=batch_size)
embeddings = siamese_nets.get_layer('branch_model').output
classification_loss = ClassificationLoss(
    loss=tf.keras.losses.categorical_crossentropy,
    similarity_layer=siamese_nets.get_layer('head_model'),
)([embeddings, labels])
trainable_model = Model([siamese_nets.get_layer('branch_model').input, labels], classification_loss)

callbacks = [
    TensorBoard(output_folder),
    ModelCheckpoint(
        str(output_folder / 'classification_loss_best_weights.h5'),
        save_best_only=True,
        save_weights_only=True,
    ),
    ReduceLROnPlateau(),
]
train_sequence = training.single.KShotNWaySequence(
    train_set,
    preprocessings=preprocessing,
    batch_size=batch_size,
    labels_in_input=True,
    labels_in_output=False,
    to_categorical=False,
    k_shot=batch_size // 8,
    n_way=8,
)
val_sequence = training.single.KShotNWaySequence(
    val_set,
    preprocessings=preprocessing,
    batch_size=batch_size,
    labels_in_input=True,
    labels_in_output=False,
    to_categorical=False,
    k_shot=batch_size // 8,
    n_way=8,
)

siamese_nets.get_layer('branch_model').trainable = False
optimizer = Adam(lr=1e-4)
trainable_model.compile(optimizer=optimizer)
trainable_model.fit_generator(
    train_sequence,
    validation_data=val_sequence,
    callbacks=callbacks,
    initial_epoch=0,
    epochs=20,
    use_multiprocessing=True,
    workers=5,
)
trainable_model.load_weights(str(output_folder / 'classification_loss_best_weights.h5'))

siamese_nets.get_layer('branch_model').trainable = True
for layer in siamese_nets.get_layer('branch_model').layers[:int(branch_depth * 0.6)]:
    layer.trainable = False
optimizer = Adam(lr=1e-5)
trainable_model.compile(optimizer=optimizer)
trainable_model.fit_generator(
    train_sequence,
    validation_data=val_sequence,
    callbacks=callbacks,
    initial_epoch=20,
    epochs=70,
    use_multiprocessing=True,
    workers=5,
)
trainable_model.load_weights(str(output_folder / 'classification_loss_best_weights.h5'))

#%% Eval on test set
k_shot = 3
n_way = 10
n_episode = 50
test_sequence = training.single.DeterministicSequence(test_set, preprocessings=preprocessing, batch_size=16)
embeddings = siamese_nets.get_layer('branch_model').predict_generator(test_sequence)

scores = []
for _ in range(n_episode):
    selected_labels = np.random.choice(test_set.label.unique(), size=n_way, replace=True)
    support_set = (
        test_set
        .loc[lambda df: df.label.isin(selected_labels)]
        .groupby('label')
        .apply(lambda group: group.sample(k_shot))
        .reset_index('label', drop=True)
    )
    query_set = (
        test_set
        .loc[lambda df: df.label.isin(selected_labels)]
        .loc[lambda df: ~df.index.isin(support_set.index)]
    )
    support_set_embeddings = embeddings[support_set.index]
    query_set_embeddings = embeddings[query_set.index]
    test_sequence = prediction.pairs.ProductSequence(
        support_images_array=support_set_embeddings,
        query_images_array=query_set_embeddings,
        support_labels=support_set.label.values,
        query_labels=query_set.label.values,
    )
    scores += [(
        test_sequence.pairs_indexes
        .assign(score=siamese_nets.get_layer('head_model').predict_generator(test_sequence, verbose=1))
        .groupby('query_index')
        .apply(lambda group: (
            group
            .sort_values('score', ascending=False)
            .assign(
                average_precision=lambda df: df.target.expanding().mean(),
                good_prediction=lambda df: df.target.iloc[0],
            )
            .loc[lambda df: df.target]
            .agg('mean')
        ))
        .agg('mean')
    )]

scores = pd.DataFrame(scores)[['score', 'average_precision', 'good_prediction']]
scores.to_csv(output_folder / 'scores.csv', index=False)