import cv2
import math
import numpy as np
import os
from scipy.ndimage import convolve
from scipy.special import gamma

from utils import reorder_image, to_y_channel,imresize

def estimate_aggd_param(block):
    """Estimate AGGD (Asymmetric Generalized Gaussian Distribution) parameters.
    Args:
        block (ndarray): 2D Image block.
    Returns:
        tuple: alpha (float), beta_l (float) and beta_r (float) for the AGGD
            distribution (Estimating the parames in Equation 7 in the paper).
    """
    block = block.flatten()
    gam = np.arange(0.2, 10.001, 0.001)  # len = 9801
    gam_reciprocal = np.reciprocal(gam)
    r_gam = np.square(gamma(gam_reciprocal * 2)) / (gamma(gam_reciprocal) * gamma(gam_reciprocal * 3))

    left_std = np.sqrt(np.mean(block[block < 0]**2))
    right_std = np.sqrt(np.mean(block[block > 0]**2))
    gammahat = left_std / right_std
    rhat = (np.mean(np.abs(block)))**2 / np.mean(block**2)
    rhatnorm = (rhat * (gammahat**3 + 1) * (gammahat + 1)) / ((gammahat**2 + 1)**2)
    array_position = np.argmin((r_gam - rhatnorm)**2)

    alpha = gam[array_position]
    beta_l = left_std * np.sqrt(gamma(1 / alpha) / gamma(3 / alpha))
    beta_r = right_std * np.sqrt(gamma(1 / alpha) / gamma(3 / alpha))
    return (alpha, beta_l, beta_r)


def compute_feature(block):
    """Compute features.
    Args:
        block (ndarray): 2D Image block.
    Returns:
        list: Features with length of 18.
    """
    feat = []
    alpha, beta_l, beta_r = estimate_aggd_param(block)
    feat.extend([alpha, (beta_l + beta_r) / 2])

    # distortions disturb the fairly regular structure of natural images.
    # This deviation can be captured by analyzing the sample distribution of
    # the products of pairs of adjacent coefficients computed along
    # horizontal, vertical and diagonal orientations.
    shifts = [[0, 1], [1, 0], [1, 1], [1, -1]]
    for i in range(len(shifts)):
        shifted_block = np.roll(block, shifts[i], axis=(0, 1))
        alpha, beta_l, beta_r = estimate_aggd_param(block * shifted_block)
        # Eq. 8
        mean = (beta_r - beta_l) * (gamma(2 / alpha) / gamma(1 / alpha))
        feat.extend([alpha, mean, beta_l, beta_r])
    return feat


def niqe(img, mu_pris_param, cov_pris_param, gaussian_window, block_size_h=96, block_size_w=96):
    """Calculate NIQE (Natural Image Quality Evaluator) metric.
    ``Paper: Making a "Completely Blind" Image Quality Analyzer``
    This implementation could produce almost the same results as the official
    MATLAB codes: http://live.ece.utexas.edu/research/quality/niqe_release.zip
    Note that we do not include block overlap height and width, since they are
    always 0 in the official implementation.
    For good performance, it is advisable by the official implementation to
    divide the distorted image in to the same size patched as used for the
    construction of multivariate Gaussian model.
    Args:
        img (ndarray): Input image whose quality needs to be computed. The
            image must be a gray or Y (of YCbCr) image with shape (h, w).
            Range [0, 255] with float type.
        mu_pris_param (ndarray): Mean of a pre-defined multivariate Gaussian
            model calculated on the pristine dataset.
        cov_pris_param (ndarray): Covariance of a pre-defined multivariate
            Gaussian model calculated on the pristine dataset.
        gaussian_window (ndarray): A 7x7 Gaussian window used for smoothing the
            image.
        block_size_h (int): Height of the blocks in to which image is divided.
            Default: 96 (the official recommended value).
        block_size_w (int): Width of the blocks in to which image is divided.
            Default: 96 (the official recommended value).
    """
    assert img.ndim == 2, ('Input image must be a gray or Y (of YCbCr) image with shape (h, w).')
    # crop image
    h, w = img.shape
    num_block_h = math.floor(h / block_size_h)
    num_block_w = math.floor(w / block_size_w)
    img = img[0:num_block_h * block_size_h, 0:num_block_w * block_size_w]

    distparam = []  # dist param is actually the multiscale features
    for scale in (1, 2):  # perform on two scales (1, 2)
        mu = convolve(img, gaussian_window, mode='nearest')
        sigma = np.sqrt(np.abs(convolve(np.square(img), gaussian_window, mode='nearest') - np.square(mu)))
        # normalize, as in Eq. 1 in the paper
        img_nomalized = (img - mu) / (sigma + 1)

        feat = []
        for idx_w in range(num_block_w):
            for idx_h in range(num_block_h):
                # process ecah block
                block = img_nomalized[idx_h * block_size_h // scale:(idx_h + 1) * block_size_h // scale,
                                      idx_w * block_size_w // scale:(idx_w + 1) * block_size_w // scale]
                feat.append(compute_feature(block))

        distparam.append(np.array(feat))

        if scale == 1:
            img = imresize(img / 255., scale=0.5, antialiasing=True)
            img = img * 255.

    distparam = np.concatenate(distparam, axis=1)

    # fit a MVG (multivariate Gaussian) model to distorted patch features
    mu_distparam = np.nanmean(distparam, axis=0)
    # use nancov. ref: https://ww2.mathworks.cn/help/stats/nancov.html
    distparam_no_nan = distparam[~np.isnan(distparam).any(axis=1)]
    cov_distparam = np.cov(distparam_no_nan, rowvar=False)

    # compute niqe quality, Eq. 10 in the paper
    invcov_param = np.linalg.pinv((cov_pris_param + cov_distparam) / 2)
    quality = np.matmul(
        np.matmul((mu_pris_param - mu_distparam), invcov_param), np.transpose((mu_pris_param - mu_distparam)))

    quality = np.sqrt(quality)
    quality = float(np.squeeze(quality))
    return quality


def calculate_niqe(img, crop_border, params_path, input_order='HWC', convert_to='y', **kwargs):
    """Calculate NIQE (Natural Image Quality Evaluator) metric.
    ``Paper: Making a "Completely Blind" Image Quality Analyzer``
    This implementation could produce almost the same results as the official
    MATLAB codes: http://live.ece.utexas.edu/research/quality/niqe_release.zip
    > MATLAB R2021a result for tests/data/baboon.png: 5.72957338 (5.7296)
    > Our re-implementation result for tests/data/baboon.png: 5.7295763 (5.7296)
    We use the official params estimated from the pristine dataset.
    We use the recommended block size (96, 96) without overlaps.
    Args:
        img (ndarray): Input image whose quality needs to be computed.
            The input image must be in range [0, 255] with float/int type.
            The input_order of image can be 'HW' or 'HWC' or 'CHW'. (BGR order)
            If the input order is 'HWC' or 'CHW', it will be converted to gray
            or Y (of YCbCr) image according to the ``convert_to`` argument.
        crop_border (int): Cropped pixels in each edge of an image. These
            pixels are not involved in the metric calculation.
        input_order (str): Whether the input order is 'HW', 'HWC' or 'CHW'.
            Default: 'HWC'.
        convert_to (str): Whether converted to 'y' (of MATLAB YCbCr) or 'gray'.
            Default: 'y'.
    Returns:
        float: NIQE result.
    """
    # ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
    # we use the official params estimated from the pristine dataset.
    niqe_pris_params = np.load(os.path.join(params_path, 'niqe_pris_params.npz'))
    mu_pris_param = niqe_pris_params['mu_pris_param']
    cov_pris_param = niqe_pris_params['cov_pris_param']
    gaussian_window = niqe_pris_params['gaussian_window']

    img = img.astype(np.float32)
    if input_order != 'HW':
        img = reorder_image(img, input_order=input_order)
        if convert_to == 'y':
            img = to_y_channel(img)
        elif convert_to == 'gray':
            img = cv2.cvtColor(img / 255., cv2.COLOR_BGR2GRAY) * 255.
        img = np.squeeze(img)

    if crop_border != 0:
        img = img[crop_border:-crop_border, crop_border:-crop_border]

    # round is necessary for being consistent with MATLAB's result
    img = img.round()

    niqe_result = niqe(img, mu_pris_param, cov_pris_param, gaussian_window)

    return niqe_result


def img_scissors(img, origin_size, dest_size):  # 将img先裁剪为origin_size*origin_size，再resize为dest_size*dest_size
    from skimage import io, transform
    from skimage.util import img_as_ubyte

    height, width = img.shape[:2]
    center_y, center_x = height // 2, width // 2
    crop_size = origin_size
    half_crop = crop_size // 2
    start_x = max(center_x - half_crop, 0)
    start_y = max(center_y - half_crop, 0)
    end_x = min(center_x + half_crop, width)
    end_y = min(center_y + half_crop, height)
    cropped_img = img[start_y:end_y, start_x:end_x]
    resized_img = transform.resize(cropped_img, (dest_size, dest_size), anti_aliasing=True)
    resized_img_ubyte = img_as_ubyte(resized_img)
    return resized_img_ubyte

def NIQE(video_origin, video_result):
    params_path = 'pre-train-models/'

    index = 0
    niqe_origin = 0.0
    niqe_result = 0.0
    
    if video_origin.isOpened() and video_result.isOpened():
        rval_origin, frame_origin = video_origin.read()  # 读取视频帧
        rval_result, frame_result = video_result.read()
    else:
        rval_origin = False
        rval_result = False

    while rval_origin and rval_result:
        
        rval_origin, frame_origin = video_origin.read()
        img_origin = img_scissors(frame_origin, 720, 512)
        rval_result, frame_result = video_result.read()
        img_result = frame_result
        if img_origin is None or img_result is None:
            break
        else:
            niqe_origin += calculate_niqe(img_origin, crop_border=0, params_path=params_path)
            niqe_result += calculate_niqe(img_result, crop_border=0, params_path=params_path)
            index += 1
    niqe_origin /= index
    niqe_result /= index
    return("The source video NIQE: " + str(niqe_origin) + "\nThe hallo genarated video NIQE: " + str(niqe_result))


if __name__ == '__main__':
    params_path = 'pre-train-models/'

    index = 0
    example_source_video_path = '../MP4/Source'
    example_hallo_video_path = '../MP4/Hallo'
    example_FID_source_img_path = '../JpgForQualitative/Macron'
    example_source_video = cv2.VideoCapture(example_source_video_path + "/Macron.mp4")
    example_hallo_video = cv2.VideoCapture(example_hallo_video_path + "/Macron.mp4")
    
    if example_source_video.isOpened() and example_hallo_video.isOpened():
        rval_source, frame_source = example_source_video.read()  # 读取视频帧
        rval_hallo, frame_hallo = example_hallo_video.read()
    else:
        rval_source = False
        rval_hallo = False

    while rval_source and rval_hallo:
        # 对视频的每一帧进行处理
        rval_source, frame_source = example_source_video.read()
        img_source = img_scissors(frame_source, 720, 512)  # 对源视频的帧图像进行尺寸统一处理
        rval_hallo, frame_hallo = example_hallo_video.read()
        img_hallo = frame_hallo
        if img_source is None or img_hallo is None:
            print("Loop End.")
            break
        else:
            if index % 100 == 0:
                cv2.imwrite(example_FID_source_img_path + "/" + str(index) + ".jpg", img_source)
                cv2.imwrite(example_FID_source_img_path + "/" + str(index) + "Res.jpg", img_hallo)
            print(index)
            index += 1
    
