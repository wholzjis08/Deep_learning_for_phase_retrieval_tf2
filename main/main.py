import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
from tensorflow.keras.preprocessing.image import ImageDataGenerator
import os
import matplotlib.pyplot as plt
from PIL import Image
from tensorflow.keras.preprocessing import image
import glob
from tensorflow.keras.layers import BatchNormalization, Conv2D, ReLU, Conv2DTranspose, add, concatenate
from scipy.io import loadmat
import numpy as np
from mobilev3 import MobileNetV3Large
from vgg_pr import VGG_PR
from tensorflow.keras.callbacks import TensorBoard
import logging
import cv2

# 参数配置
img_size = (299,299)
batch_size = 8
num_label = 20
initial_lr = 0.001
total_epoch = 100
repeat_times = 1
total_train = 10000
# 编号
case_num = 9
# gpu设置
gpus = tf.config.experimental.list_physical_devices('GPU')
tf.config.experimental.set_virtual_device_configuration(
            gpus[0],
            [tf.config.experimental.VirtualDeviceConfiguration(memory_limit=4096)])

os.chdir(os.getcwd())

train_img_list = sorted(glob.glob('../dataset/train/intensity/*.mat'))
train_label_list = sorted(glob.glob('../dataset/train/phase/*.txt'))
val_img_list = sorted(glob.glob('../dataset/validate/intensity/*.mat'))
val_label_list = sorted(glob.glob('../dataset/validate/phase/*.txt'))
ckpt_path = '../checkpoints/VGG-{epoch}.ckpt'
log_path = '../log/{}/'
if not os.path.exists(log_path.format(case_num)):
    os.mkdir(log_path.format(case_num))

# read data
####################### 利用tf.data高级API进行数据读取 ########################
def read_img(filename):
    # print("------------------------{}".format(filename))
    image_dict = loadmat(filename.decode('utf-8'))
    # process 1 原图归一化
    # image_decoded = image_dict['Iz'] /4e-4  # 归一化
    # image_resized = np.float32(np.expand_dims(image_decoded, axis=-1))
    ### process 2 过曝图归一化
    # image_decoded = image_dict['Iz']
    # image_decoded[image_decoded>2e-4] = 2e-4
    # image_decoded /= 2e-4
    # image_resized = np.float32(np.expand_dims(image_decoded, axis=-1))
    # process 3 降采样，可以提升batch_size
    exp_thresh = 1e4
    image_decoded = image_dict['Iz']
    image_decoded = cv2.resize(image_decoded, img_size, interpolation=cv2.INTER_AREA)
    image_decoded[image_decoded>exp_thresh] = exp_thresh
    image_decoded /= exp_thresh
    image_resized = np.float32(np.expand_dims(image_decoded, axis=-1))
    # image_resized = tf.convert_to_tensor(image_resized)
    return image_resized

def read_label(filename):
    label = open(filename).read()
    # print(label)
    label = label.strip().split(' ')
    label = [np.float32(i) for i in label if i!='']
    label = np.reshape(label, [1,1,-1])
    label = np.array(label) + 0.5  # 归一化
    # label = tf.convert_to_tensorf(label)
    return label

# plt.figure()
# img = read_img('E:/00_PhaseRetrieval/PhENN/dataset/train/intensity/image000010.mat')
# print(tf.reduce_max(img))
# plt.imshow(tf.squeeze(np.log(img)), cmap='gray')
# plt.show()
# print(read_label('E:/00_PhaseRetrieval/PhENN/dataset/train/phase/image000010.txt'))

# 这个函数将作为map的输入，因此尽管label看似输入后没有用到，但也必须写进来
def parse_function(image_filename, label_filename):
    # print('--------------{}-------------------'.format(tf.as_string(image_filename))) # filename变成tensor了？
    img = tf.numpy_function(read_img, [image_filename], tf.float32)
    label = tf.numpy_function(read_label, [label_filename], tf.float32)
    return img, label

###### 打印检查滑动平均值
def get_bn_vars(collection):

    moving_mean, moving_variance = None, None
    for var in collection:
        name = var.name.lower()
        if "variance" in name:
            moving_variance = var
        if "mean" in name:
            moving_mean = var
    if moving_mean is not None and moving_variance is not None:
        return moving_mean, moving_variance
    raise ValueError("Unable to find moving mean and variance")
###
# @tf.function
def train_step(model, tdataset, epoch, loss_object, train_loss, optimizer, writer):
    try:
        for batch, data in enumerate(tdataset):
            images, labels = data
            with tf.GradientTape() as tape:
                pred = model(images, training=True)
                # pred = tf.squeeze(pred)
                if len(pred.shape) == 2:
                    pred = tf.reshape(pred,[-1, 1, 1, num_label])
                # check out shapes
                # print("the shape of network output: {}\n the shape of label: {}".format(pred.shape, labels.shape))
                # print("train pred :{}\ntrain_label: {}".format(pred, labels))
                loss = loss_object(pred, labels)
            gradients = tape.gradient(loss, model.trainable_variables)
            optimizer.apply_gradients(zip(gradients, model.trainable_variables))
            # mean, variance = get_bn_vars(model.variables)
            # print("mean: {}".format(mean))
            # print("variance: {}".format(variance))
            if batch % 20 ==0:
                # result() computes and returns the metric value tensor.
                logging.info('Epoch: {}, iter: {}, loss:{}'.format(epoch, batch, loss.numpy()))
            tf.summary.scalar('train_loss', loss.numpy(), step=epoch*int(total_train/batch_size)*repeat_times+batch)      # the tdataset has been repeated 5 times..
            tf.summary.text('Zernike_coe_pred', tf.as_string(tf.squeeze(pred)), step=epoch*int(total_train/batch_size)*repeat_times+batch)
            tf.summary.text('Zernike_coe_gt', tf.as_string(tf.squeeze(labels)), step=epoch*int(total_train/batch_size)*repeat_times+batch)
            # tf.summary.image('input_intensity', tf.math.log(images), step=epoch*5000+batch, max_outputs=1)
            writer.flush()
            train_loss(loss)
        return train_loss
        # model.save_weights(ckpt_path.format(epoch=epoch))
    except KeyboardInterrupt:
        logging.info('interrupted.')
        model.save_weights(ckpt_path.format(epoch=epoch))
        logging.info('model saved into {}'.format(ckpt_path.format(epoch=epoch)))
        exit(0) # 无异常退出

# @tf.function
def val_step(model, vdataset, epoch, val_loss_object, val_loss):
    for batch, data in enumerate(vdataset):
        images, labels = data
        # print("images:{}".format(images))
        val_pred = model(images, training=True)
        if len(val_pred.shape) == 2:
            val_pred = tf.reshape(val_pred,[-1, 1, 1, num_label])
        # print("val pred :{}\nval_label: {}".format(val_pred, labels))
        # mean, variance = get_bn_vars(model.variables)
        # print("val mean: {}".format(mean))
        # print("val variance: {}".format(variance))
        v_loss = val_loss_object(val_pred, labels)
        val_loss(v_loss)
    return val_loss
#####################
def train():
    logging.basicConfig(level=logging.INFO)
    tdataset = tf.data.Dataset.from_tensor_slices((train_img_list, train_label_list))
    tdataset = tdataset.map(parse_function, 3).shuffle(buffer_size=200).batch(batch_size).repeat(repeat_times)
    vdataset = tf.data.Dataset.from_tensor_slices((val_img_list, val_label_list))
    # vdataset = tf.data.Dataset.from_tensor_slices((train_img_list[:2000], train_label_list[:2000]))
    vdataset = vdataset.map(parse_function, 3).batch(1)

    ### Mobilenet model
    # base_model = MobileNetV3Large(classes=num_label)
    # model = base_model
    ### Vgg model
    model = VGG_PR(num_classes=num_label)
    ### compling model ###
    # input = tf.keras.layers.Input(shape=(img_size[0],img_size[1],1))
    # output = model(input)
    # model = tf.keras.models.Model(input, output)

    logging.info('Model loaded')

    start_epoch = 0
    # 该函数返回 the full path to the latest checkpoint，是string
    latest_ckpt = tf.train.latest_checkpoint(os.path.dirname(ckpt_path))
    if latest_ckpt:
        start_epoch = int(latest_ckpt.split('-')[1].split('.')[0])
        model.load_weights(latest_ckpt)
        logging.info('model resumed from: {}, start at epoch: {}'.format(latest_ckpt, start_epoch))
    else:
        logging.info('training from scratch since weights no there')

    ######## 用自定义loop进行训练 ########
    loss_object = tf.keras.losses.MeanSquaredError()
    val_loss_object = tf.keras.losses.MeanSquaredError()
    optimizer = tf.keras.optimizers.Adam(learning_rate=initial_lr)
    train_loss = tf.metrics.Mean(name='train_loss') # 表示对所有训练损失求平均
    val_loss = tf.metrics.Mean(name='val_loss')
    writer = tf.summary.create_file_writer(log_path.format(case_num))

    with writer.as_default():
        for epoch in range(start_epoch, total_epoch):
            print('start training')
            train_loss = train_step(model, tdataset, epoch, loss_object, train_loss, optimizer, writer)
            val_loss = val_step(model, vdataset, epoch, val_loss_object, val_loss)

            logging.info('Epoch: {}, average train_loss:{}, val_loss: {}'.format(epoch, train_loss.result(), val_loss.result()))
            model.save_weights(ckpt_path.format(epoch=epoch))
            tf.summary.scalar('val_loss', val_loss.result(), step = epoch)
            writer.flush()
            train_loss.reset_states()
            val_loss.reset_states()
        model.save_weights(ckpt_path.format(epoch=epoch))

if __name__ == "__main__":
    train()
