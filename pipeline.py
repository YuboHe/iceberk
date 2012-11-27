"""Yangqing's refactoring of the CVPR'12 algorithm

In this program we will represent any general "image" with a width * height * 
nchannels numpy matrix of np.float64, , which is always preserved as a
contiguous array in C-order so we can more efficiently solve most of the
problems.
"""
from iceberk import cpputil, mathutil, mpi, util
from iceberk import kmeans_mpi, omp_mpi, omp_n_mpi
import logging
import numpy as np
from PIL import Image
from sklearn import metrics

# we try to import bottleneck: this helps computing the nearest neighbors in 
# LLC faster. Otherwise, we will simply use np.argsort.
try:
    import bottleneck as bn
except ImportError:
    logging.warning('Cannot find bottleneck, using numpy as backup.')
    bn = None

def CHECK_IMAGE(img):
    if (type(img) is np.ndarray) and (img.ndim == 3) \
            and (img.dtype == np.float64):
        pass
    else:
        raise RuntimeError, "The image format is incorrect."


class Component(object):
    """ The common interface to process an input image
    
    Components could be used to stack up a big convolutional layer. See 
    ConvLayer for more details.
    """
    def __init__(self, specs):
        """ initialize with some specifications
        
        Input:
            specs: a dictionary containing param keywords and values
        """
        self.specs = specs
    
    def process(self, image):
        """ The interface that processes the input from the last component,
        and outputs the input for the next component.
        
        Input:
            image: an Ndarray where the feature is along the last axis
        Output:
            output: an Ndarray, whose size only differes from image on the
                last dimension. It should be the output to the next layer.
        """
        raise NotImplementedError

    def train(self, patches):
        """ The interface that trains the component.
        
        Input:
            patches: a 2-D numpy array (unlike image!)
        Output:
            output: the 2-D numpy array as the output of this component
        """
        raise NotImplementedError

class ConvLayer(list):
    """ ConvLayer is one big layer in the convolutional pipeline.
    
    It starts with a patch extractor, followed by several feature processing
    components, and ends with a spatial pooler.
    """
    def __init__(self, *args, **kwargs):
        """Initialize a convolutional layer.
        Optional keyword parameters:
            prev: the previous convolutional layer. Default None.
        """
        self._previous_layer = kwargs.pop('prev', None)
        super(ConvLayer, self).__init__(*args, **kwargs)
        
    def train(self, dataset, num_patches,
              exhaustive = False, ratio_per_image = 0.1):
        """ train the convolutional layer
        
        Note that we do not train the first element (patch extractor),
        and stop when we see the spatial pooler. There might be some post
        processing components after the pooler, but they should not require
        any training (if they do, you may want to move them to the next layer
        """
        if len(self) == 0:
            return
        logging.debug("Training convolutional layer...")
        if not isinstance(self[0], Extractor):
            raise ValueError, \
                  "The first component should be a patch extractor!"
        patches = self[0].sample(dataset, num_patches, self._previous_layer,
                                 exhaustive, ratio_per_image)
        if len(self) == 1 or isinstance(self[1], Pooler):
            logging.debug('Nothing to be trained in this layer.')
            return
        # actually train the model
        for i in range(1, len(self)):
            component = self[i]
            mpi.barrier()
            logging.debug("Training %s..." % (component.__class__.__name__))
            component.train(patches)
            if i == len(self) - 1 or isinstance(self[i+1], Pooler):
                # if we've reached a pooler, stop training
                break
            else:
                # prepare the next component's input
                patches = component.process(patches)
        logging.debug("Training convolutional layer done.")
        
        
    def process(self, image, as_vector = False):
        output = image
        if self._previous_layer is not None:
            output = self._previous_layer.process(image)
        for element in self:
            output = element.process(output)
        if as_vector:
            output.resize(np.prod(output.shape))
        return output
    
    def process_dataset(self, dataset, as_list = False, as_2d = False):
        """Processes a whole dataset and returns an numpy ndarray
        
        Input:
            dataset: the input dataset.
            as_list: if True, return a list. This applies when the output has
                different sizes for each image. Default False.
            as_2d: if True, return a matrix where each image corresponds to a
                row in the matrix. Default False.
        """
        total = dataset.size_total()
        logging.debug("Processing a total of %s images" % (total,))
        timer = util.Timer()
        if as_list:
            data = [self.process(dataset.image(i)) for i in range(dataset.size())]
        else:
            # we assume that each image leads to the same feature size
            temp = self.process(dataset.image(0), as_vector = as_2d)
            logging.debug("Output feature shape: %s" % (str(temp.shape)))
            data = np.empty((dataset.size(),) + temp.shape)
            data[0] = temp
            for i in range(1,dataset.size()):
                data[i] = self.process(dataset.image(i), as_vector = as_2d)
        mpi.barrier()
        logging.debug("Feature extration took %s" % timer.total())
        return data
    
    def sample(self, dataset, num_patches,
               exhaustive = False, ratio_per_image = 0.1):
        """Sample pooled features from the dataset. For example, if after
        pooling, the output feature is 4*4*1000, then the sampled output is
        num_patches * 1000.
        """
        extractor = IdenticalExtractor()
        return extractor.sample(dataset, num_patches, self, 
                                exhaustive, ratio_per_image)

class Extractor(Component):
    """Extractor is just an abstract class that holds all extractor subclasses
    """
    def train(self, incoming_patches):
        raise RuntimeError,\
            "You should not call the train() function of a extractor."
    
    def sample(self, dataset, num_patches, previous_layer = None,
               exhaustive = False, ratio_per_image = 0.1):
        """ randomly sample num_patches from the dataset. Pass previous_layer
        if sampling should be performed on the output of a previously computed
        layer.
        
        The returned patches would be a 2-dimensional ndarray of size
            [num_patches, psize[0] * psize[1] * num_channels]
        When we sample patches, we need to process all the images, which might
        not be a very efficient way.
        
        In default, exhaustive is set False so that the sampling is carried out
        in a lazy way - for each image we keep a subset of its features given
        by ratio_per_image, and as soon as we hit the number of required patches
        we stop sampling.
        """
        logging.debug("Extracting %d patches..." % num_patches)
        num_patches = np.maximum(int(num_patches / float(mpi.SIZE) + 0.5), 1)
        sampler = mathutil.ReservoirSampler(num_patches)
        order = np.arange(dataset.size())
        if not exhaustive:
            order = np.random.permutation(order)
        for i in range(dataset.size()):
            if previous_layer is not None:
                feat = previous_layer.process(dataset.image(i))
            else:
                feat = dataset.image(i)
            feat = self.process(feat)
            feat.resize((np.prod(feat.shape[:2]),) + feat.shape[2:])
            if exhaustive:
                sampler.consider(feat)
            else:
                # randomly keep ratio_per_image
                idx = np.random.permutation(np.arange(feat.shape[0]))
                num_selected = max(int(feat.shape[0] * ratio_per_image), 1)
                sampler.consider(feat[idx[:num_selected]])
                # as soon as we hit the number of patches needed, quit
                if sampler.num_considered() > num_patches:
                    break
        if sampler.num_considered() < num_patches:
            logging.warning("Warning: the number of provided patches is " \
                            "smaller than the number of samples needed.")
        return sampler.get()
    
    def process(self, image):
        """Each extractor should implement its own process function
        """
        raise NotImplementedError
            
class IdenticalExtractor(Extractor):
    """A dummy extractor that simply extracts the image itself
    """
    def __init__(self):
        pass
    
    def process(self, image):
        return np.atleast_3d(image.copy())

class PatchExtractor(Extractor):
    """The patch extractor. It densely extracts overlapping patches, and 
    convert them as an NDarray which could be passed on to different 
    components.
    """
    def __init__(self, psize, stride):
        """Initialize a patch extractor
        
        Input:
        psize: the patch size, [w,h] for rectangular patches, or a single int
            for square patches.
        stride: the stride for dense extraction
        """
        if type(psize) is int:
            self.psize = [psize,psize]
        else:
            self.psize = psize
        self.stride = stride
    
    def sample(self, dataset, num_patches, previous_layer = None,
               exhaustive = False, ratio_per_image = 0.1):
        """ randomly sample num_patches from the dataset.
        
        The returned patches would be a 2-dimensional ndarray of size
            [num_patches, psize[0] * psize[1] * num_channels]
        """
        if previous_layer is not None:
            return Extractor.sample(self, dataset, num_patches,
                                    previous_layer, exhaustive, ratio_per_image)
        # if there is no previous layer, we have a more efficient method to
        # perform sampling.
        num_patches = np.maximum(int(num_patches / float(mpi.SIZE) + 0.5), 1)
        imids = np.random.randint(dataset.size(), size=num_patches)
        # sort the ids so we don't need to re-read images when sampling
        imids.sort()
        patches = np.empty((num_patches, 
                            self.psize[0] * 
                            self.psize[1] * 
                            dataset.num_channels()))
        current_im = -1
        if dataset.dim() is not None:
            # all images have the same dim, making random sampling easier
            dim = dataset.dim()
            rowids = np.random.randint(dim[0]-self.psize[0], size=num_patches)
            colids = np.random.randint(dim[1]-self.psize[1], size=num_patches)
            precomputed = True  
        else:
            precomputed = False
        for i in range(num_patches):
            if imids[i] != current_im:
                im = dataset.image(imids[i])
                current_im = imids[i]
            if not precomputed:
                rowid = np.random.randint(im.shape[0]-self.psize[0])
                colid = np.random.randint(im.shape[1]-self.psize[1])
            else:
                rowid = rowids[i]
                colid = colids[i]
            patches[i] = im[rowid:rowid+self.psize[0], \
                            colid:colid+self.psize[1]].flat
        return patches
        
    def process(self, image):
        '''process an image
        
        The returned image would be a 3-dimensional ndarray of size
            [new_height, new_width, psize[0] * psize[1] * num_channels]
        '''
        image = np.atleast_3d(image)
        imheight = image.shape[0]
        imwidth = image.shape[1]
        num_channels = image.shape[2]
        stride = self.stride
        idxh = range(0,imheight-self.psize[0]+1,stride)
        idxw = range(0,imwidth-self.psize[1]+1,stride)
        new_height, new_width = len(idxh), len(idxw)
        num_patches= len(idxh) * len(idxw)
        if num_patches == 0:
            raise ValueError, "The image is too small for dense extraction!"
        patches = np.empty((new_height, new_width, 
                            self.psize[0] * 
                            self.psize[1] * 
                            num_channels))
        for i in idxh:
            for j in idxw:
                patches[i,j] = image[i:i+self.psize[0],j:j+self.psize[1]].flat
        return patches
        

class Normalizer(Component):
    """ Normalizer are those layers that do not need training
    """
    
    def process(self, image):
        raise NotImplementedError
    
    def train(self, patches):
        """ For normalizers, usually no training should be needed.
        """
        pass


class MeanNormalizer(Normalizer):
    """Normalizes the patches to mean zero
    """
    def process(self, image):
        """ normalizes the patches.
        """
        image = image.astype(np.float64)
        m = image.mean(axis=-1).reshape(image.shape[:-1] + (1,))
        image_out = image - m
        return image_out


class MeanvarNormalizer(Normalizer):
    """Normalizes the patches to mean zero and standard deviation 1
    
    Specs:
        'reg': the regularization term added to the norm.
    """
    def process(self, image):
        """ normalizes the patches.
        """
        image = image.astype(np.float64)
        shape_old = image.shape
        shape_temp = (np.prod(shape_old[:-1]), shape_old[-1])
        image.resize(shape_temp)
        m = image.mean(axis=1)
        std = image.std(axis=1)
        std += self.specs.get('reg', np.finfo(np.float64).eps)
        image_out = image - m[:, np.newaxis]
        image_out /= std[:, np.newaxis]
        image.resize(shape_old)
        image_out.resize(shape_old)
        return image_out

class SpatialMeanNormalizer(Normalizer):
    """Normalizes the patches by subtracting the per-channel mean.
    
    Specs:
        'channels': the number of channels for the patches.
    """
    def process(self, image):
        """ normalizes the patches.
        """
        image = image.astype(np.float64)
        channels = self.specs['channels']
        shape_old = image.shape
        # first, subtract the mean
        shape_temp = (np.prod(shape_old[:-1]), 
                      shape_old[-1] / channels, channels)
        image.resize(shape_temp)
        m = image.mean(axis=1)
        image_out = image - m[:,np.newaxis, :]
        image.resize(shape_old)
        image_out.resize(shape_old)
        return image_out


class SpatialMeanvarNormalizer(Normalizer):
    """Normalizes the patches by subtracting the per-channel mean, and standard
    deviation 1.
    
    Specs:
        'reg': the regularization term added to the norm.
        'channels': the number of channels for the patches.
    """
    def process(self, image):
        """ normalizes the patches.
        """
        image = image.astype(np.float64)
        channels = self.specs['channels']
        shape_old = image.shape
        # first, subtract the mean
        shape_temp = (np.prod(shape_old[:-1]), 
                      shape_old[-1] / channels, channels)
        image.resize(shape_temp)
        m = image.mean(axis=1)
        image_out = image - m[:,np.newaxis, :]
        # then, do normalization
        shape_temp = (np.prod(shape_old[:-1]), shape_old[-1])
        image_out.resize(shape_temp)
        length = (image_out**2).sum(1)
        length += self.specs.get('reg', np.finfo(np.float64).eps)
        image_out /= length[:, np.newaxis]
        image.resize(shape_old)
        image_out.resize(shape_old)
        return image_out


class L2Normalizer(Normalizer):
    """Normalizes the patches so they lie on a unit ball.
    
    Specs:
        'reg': the regularization term added to the norm.
    """
    def process(self, image):
        """ normalizes the patches
        """
        shape_old = image.shape
        shape_temp = (np.prod(shape_old[:-1]), shape_old[-1])
        image.resize(shape_temp)
        length = np.sqrt((image**2).sum(axis=1))
        length += self.specs.get('reg', np.finfo(np.float64).eps)
        image_out = image / length[:, np.newaxis]
        image.resize(shape_old)
        image_out.resize(shape_old)
        return image_out


class L1Normalizer(Normalizer):
    """Normalizes the patches so each patch sums to 1
    
    Specs:
        'reg': the regularization term added to the norm.
    """
    def process(self, image):
        """ normalizes the patches
        """
        reg = self.specs.get('reg', np.finfo(np.float64).eps)
        image_out = image / (np.sum(image, axis = -1) + reg).\
                                reshape(image.shape[:-1] + (1,))
        return image_out
    

class DictionaryTrainer(object):
    """The dictionary trainer
    """
    def __init__(self, specs):
        """ initialize with some specifications
        
        Input:
            specs: a dictionary containing param keywords and values
        """
        self.specs = specs
        
    def train(self, incoming_patches):
        """ train a dictionary, and return the necessary dictionary parameters
        
        Input:
            incoming_patches: a 2-d matrix each row being a patch feature vector
            specs: a dictionary of specification parameters
        Output:
            dictionary: the dictionary items
            misc: misc variables that might be useful in inspection.
        """
        raise NotImplementedError
    
class PcaTrainer(DictionaryTrainer):
    """Performs PCA training
    """
    def train(self, incoming_patches):
        m, covmat = mathutil.mpi_meancov(incoming_patches)
        if mpi.is_root():
            # only root carries out the computation
            eigval, eigvec = np.linalg.eigh(covmat)
            reg = self.specs.get('reg', np.finfo(np.float64).eps)
            W = eigvec * 1.0 / (np.sqrt(np.maximum(eigval, 0.0)) + reg)
        else:
            eigval, eigvec, W = None, None, None
        W = mpi.COMM.bcast(W)
        eigval = mpi.COMM.bcast(eigval)
        eigvec = mpi.COMM.bcast(eigvec)
        return (W, -m), (eigval, eigvec, covmat)

class ZcaTrainer(PcaTrainer):
    """Performs ZCA training
    """
    def train(self, incoming_patches):
        (W, b), (eigval, eigvec, covmat) = PcaTrainer.train(self, incoming_patches)
        W = np.dot(W, eigvec.T)
        return (W, b), (eigval, eigvec, covmat)


class KmeansTrainer(DictionaryTrainer):
    """KmeansTrainer Performs Kmeans training
    specs:
        k: the number of kmeans centers
        n_init: number of indepedent kmeans tries (default 1)
        max_iter: the maximum mumber of kmeans iterations (default 100)
        tol: the tolerance threshold before we stop iterating (default 1e-4)
    """
    def train(self, incoming_patches):
        centroids, label, inertia = \
            kmeans_mpi.kmeans(incoming_patches, 
                              self.specs['k'],
                              n_init = self.specs.get('n_init', 1),
                              max_iter = self.specs.get('max_iter', 100),
                              tol = self.specs.get('tol', 0.0001))
        return centroids, (label, inertia)

class NormalizedKmeansTrainer(KmeansTrainer):
    """NormalKmeansTrainer Performs Kmeans training, but returns normalized
    dictionary entries (i.e. each entry has length 1)
    specs:
        k: the number of kmeans centers
        n_init: number of indepedent kmeans tries (default 1)
        max_iter: the maximum mumber of kmeans iterations (default 100)
        tol: the tolerance threshold before we stop iterating (default 1e-4)
    """
    def train(self, incoming_patches):
        centroids, misc = KmeansTrainer.train(self, incoming_patches)
        centroids /= np.sqrt((centroids**2).sum(1))[:, np.newaxis]
        return centroids, misc

class OMPTrainer(DictionaryTrainer):
    """Orthogonal Matching Pursuit
    """
    def train(self, incoming_patches):
        centroid = omp_mpi.omp1(incoming_patches,
                                self.specs['k'],
                                max_iter = self.specs.get('max_iter', 100),
                                tol = self.specs.get('tol', 0.0001)
                                )
        return centroid, ()

class OMPNTrainer(DictionaryTrainer):
    """Orthogonal Matching Pursuit with N activations instead of 
    """
    def train(self, incoming_patches):
        centroid = omp_n_mpi.omp_n(incoming_patches,
                                   self.specs['k'],
                                   self.specs['num_active'],
                                   max_iter = self.specs.get('max_iter', 100),
                                   tol = self.specs.get('tol', 0.0001)
                                  )
        return centroid, ()

class FeatureEncoder(Component):
    """The feature encoder.
    
    The old PatchPreprocessor in jiayq.imageclassify is now part of 
    PatchEncoder. The old DictTrainer is now absorbed into PatchEncoder.
    """
    def __init__(self, specs, trainer = None):
        self.trainer = trainer
        self.dictionary = None
        super(FeatureEncoder, self).__init__(specs)
        
    def process(self, image):
        raise NotImplementedError
        
    def train(self, incoming_patches):
        if self.trainer is not None:
            self.dictionary = self.trainer.train(incoming_patches)[0]

class LinearEncoderBW(FeatureEncoder):
    """A linear encoder that does output = (input + b) * W
    """
    def process(self, image):
        W, b = self.dictionary
        # we create the offset in-place: this might introduce some numerical
        # differences but should be fine most of the time
        image += b
        output = mathutil.dot_image(image, W)
        image -= b
        return output

class LinearEncoderWB(FeatureEncoder):
    """A linear encoder that does output = input * W + b
    """
    def process(self, image):
        W, b  = self.dictionary
        output = mathutil.dot_image(image, W)
        output += b
        return output

"""the default linear encoder is LinearEncoderBW
"""
LinearEncoder = LinearEncoderBW

class InnerProductEncoder(FeatureEncoder):
    """ An innner product encoder that does output = np.dot(input, dictionary)
    """
    def process(self, image):
        return mathutil.dot_image(image, self.dictionary.T)

class VQEncoder(FeatureEncoder):
    """ Vector quantization encoder
    """
    def process(self, image):
        shape = image.shape[:-1]
        num_channels = image.shape[-1]
        image_2d = image.reshape((np.prod(shape), num_channels))
        distance = metrics.euclidean_distances(image_2d, self.dictionary)
        output = np.zeros_like(distance)
        idx = distance.argmin(axis=1)
        output[:,idx] = 1
        return output.reshape(shape + (output.shape[-1],))

class ThresholdEncoder(FeatureEncoder):
    """ Like inner product encoder, but does thresholding to zero-out small
    values.
    """
    def process(self, image):
        # 0.25 is the default value used in Ng's paper
        alpha = self.specs.get('alpha', 0.25)
        output = mathutil.dot_image(image, self.dictionary.T)
        # check if we would like to do two-side thresholding. Default yes.
        if self.specs.get('twoside', True):
            # concatenate, and make sure to be C_CONTIGUOUS
            imshape = output.shape[:-1]
            N = output.shape[-1]
            output.resize((np.prod(imshape), N))
            temp = np.empty((np.prod(imshape), N*2))
            temp[:,:N] = output
            temp[:,N:] = -output
            output = temp.reshape(imshape + (N*2,))
        elif self.specs['twoside'] == 'abs':
            np.abs(output, out=output)
        else:
            pass
        output -= alpha
        np.clip(output, 0., np.inf, out=output)
        return output


class ReLUEncoder(ThresholdEncoder):
    """ ReLUEncoder is simply the threshold encoder with the alpha term set to
    zero.
    """
    def __init__(self, *args, **kwargs):
        super(ReLUEncoder, self).__init__(*args, **kwargs)
        self.specs['alpha'] = 0.


class TriangleEncoder(FeatureEncoder):
    """ Does triangle encoding as described in Coates and Ng's AISTATS paper
    """
    def process(self, image):
        shape = image.shape[:-1]
        num_channels = image.shape[-1]
        image_2d = image.reshape((np.prod(shape), num_channels))
        distance = metrics.euclidean_distances(image_2d, self.dictionary)
        mu = np.mean(distance, axis=1)
        encoded = np.maximum(0., mu.reshape(mu.size, 1) - distance)
        return encoded.reshape(shape + (encoded.shape[-1],))
                    
class LLCEncoder(FeatureEncoder):
    """Encode with LLC
    
    specs:
         k: the number of LLC nearest neighbors. default 5.
         reg: the LLC reconstruction regularize. default 1e-4.
         (default values from Jianchao Yang's LLC paper in CVPR 2010)
    """
    def process(self, image):
        '''Performs llc encoding.
        '''
        K = self.specs.get('k', 5)
        reg = self.specs.get('reg', 1e-4)
        D = self.dictionary
        shape = image.shape[:-1]
        X = image.reshape((np.prod(shape), image.shape[-1]))
        # D_norm is the precomputed norm of the entries
        if 'D_norm' not in self.specs:
            self.specs['D_norm'] = (D**2).sum(1) / 2.
        D_norm = self.specs['D_norm']
        distance = mathutil.dot(X, -D.T)
        distance += D_norm
        # find the K closest indices
        if bn is not None:
            # use bottleneck which would be faster
            IDX = bn.argpartsort(distance, K, axis=1)[:, :K]
        else:
            IDX = np.argsort(distance,1)[:, :K]
        # do LLC approximate coding
        coeff = np.zeros((X.shape[0], D.shape[0]))
        ONES = np.ones(K)
        Z = np.empty((K, D.shape[1]))
        for i in range(X.shape[0]):
            # shift to origin
            Z[:] = D[IDX[i]]
            Z -= X[i]
            # local covariance
            C = mathutil.dot(Z,Z.T)
            # add regularization
            C.flat[::K+1] += reg * C.trace()
            w = np.linalg.solve(C,ONES)
            coeff[i][IDX[i]] = w / w.sum()
        return coeff.reshape(shape + (coeff.shape[1],))

class Pooler(Component):
    """Pooler is just an abstract class that holds all pooling subclasses
    """
    def __init__(self, specs):
        Component.__init__(self, specs)
        
    def train(self, incoming_patches):
        raise RuntimeError,\
            "You should not call the train() function of a pooler."


class MetaPooler(Pooler):
    """MetaPooler is a wrapper that combines the output of multiple simple
    poolers.
    """
    def __init__(self, basic_poolers, specs={}):
        """Initialize with a list of basic poolers
        """
        self._basic_poolers = basic_poolers
        self.specs = specs

    def process(self, image):
        output = []
        for basic_pooler in self._basic_poolers:
            output.append(basic_pooler.process(image).flatten())
        return np.hstack(output)

class SpatialPooler(Pooler):
    """ The spatial Pooler that does spatial pooling on a regular grid.
    specs:
        grid: an int or a tuple indicating the pooling grid.
        method: 'max', 'ave' or 'rms'.
    """
    
    def set_grid(self, grid):
        """ The function is provided in case one needs to change the grid of
        the spatial pooler on the fly
        """
        self.specs['grid'] = grid
    
    def process(self, image):
        if not (image.flags['C_CONTIGUOUS'] and image.dtype == np.float64):
            logging.warning("Warning: the image is not contiguous.")
            image = np.ascontiguousarray(image, dtype=np.float64)
        # do fast pooling
        grid = self.specs['grid']
        if type(grid) is int:
            grid = (grid, grid)
        output = cpputil.fastpooling(image, grid, self.specs['method'])
        return output

class PyramidPooler(MetaPooler):
    """PyramidPooler performs pyramid pooling.
    
    The current code is a hack by stacking spatial poolers. In the future we
    should write it in a more efficient way.
    
    specs:
        level: an int indicating the number of pyramid levels. For example, 3
            means performing 1x1, 2x2 and 4x4 pooling. Alternately, specify a
            list of levels, e.g., [0,2] to specify 1x1 (2^0) and 4x4 (2^2)
            pooling.
        method: 'max', 'ave' or 'rms'.
    """
    def __init__(self, specs):
        basic_poolers = []
        level = specs['level']
        if type(level) is int:
            level = range(level)
        for i in level:
            basic_poolers.append(
                    SpatialPooler({'grid': 2**i, 'method': specs['method']}))
        super(PyramidPooler, self).__init__(basic_poolers, specs)


class FixedSizePooler(Pooler):
    """FixedSizePooler is similar to SpatialPooler, but instead of using a grid
    that adapts to the image size, it uses a fixed receptive field to pool 
    features from. If the input image size (minus the size) is not a multiple
    of the stride, the boundaries are evenly removed from each side.
    
    specs:
        size: an int, or a 2-tuple indicating the size of each pooled feature
            receptive field.
        method: 'max', 'ave' or 'rms'
    """
    def __init__(self, specs):
        Pooler.__init__(self, specs)
        size = self.specs['size']
        if type(size) is int:
            self.specs['size'] = (size, size)
        # in the end, convert them to numpy arrays for easier indexing
        self.specs['size'] = np.asarray(self.specs['size'], dtype = int)
        self._spatialpooler = SpatialPooler({'method': specs['method']})

    def process(self, image):
        """process an image. If the input image size does not fit the pooling
        region (multiples of grid), the boundary is cut as evenly as possible
        around the border.
        """
        image_size = np.asarray(image.shape[:2])
        grid = (image_size / self.specs['size']).astype(int)
        pool_size = grid * self.specs['size']
        offset = ((image_size - pool_size) / 2).astype(int)
        # we use a spatial pooler to do the actual job
        image = np.ascontiguousarray(image[offset[0]:offset[0]+pool_size[0],
                                           offset[1]:offset[1]+pool_size[1]],
                                     dtype = np.float64)
        self._spatialpooler.set_grid(grid)
        return self._spatialpooler.process(image)

class KernelPooler(Pooler):
    """KernelPooler is similar to SpatialPooler but uses a kernel to weight
    different locations, and can also apply more complex feature transforms
    such as second order pooling on the data.
    
    specs:
        kernel: a 2D numpy array, non-negative
        stride: the stride with which this kernel should be carried out
        method: 'ave' or 'rms'. You can also use 'max' which finds the max value
            after the weighting, but I feel that it's not very well-defined. You
            can also pass in an object that carries out more complex feature
            computations, in which case the object should have two functions,
            method.dim(x) that returns the output dimension based on the input
            dimension, and method.pool(X, output) that processes pooling and puts
            the result in the vector output.
    """
    def __init__(self, specs):
        Pooler.__init__(self, specs)
        # we disabled the kernel normalization part: you are responsible of
        # making sure that the kernel is right.
        #kernel = self.specs['kernel']
        #np.clip(kernel, 0, np.inf, out=kernel)
        #s = kernel.sum()
        #if s <= 0:
        #    raise ValueError, "The kernel does not seem to be right"
        #kernel /= s
        stride = self.specs['stride']
        if type(stride) is int:
            self.specs['stride'] = (stride, stride)
        self.specs['stride'] = np.asarray(self.specs['stride'], dtype=int)
    
    @staticmethod
    def max(X, out):
        return np.max(X, axis=0, out=out)
    
    @staticmethod
    def ave(X, out):
        return np.mean(X, axis=0, out=out)
    
    @staticmethod
    def rms(X, out):
        # note that we will destroy X in the process
        X **= 2
        out = np.mean(X, axis=0, out=out)
        np.sqrt(out, out=out)
        return out
    
    def process(self, image):
        method = self.specs['method']
        # if method is not pre-defined, it should be an object that can be
        # called to execute the function.
        # convert the default methods to functions
        output_dim = image.shape[-1]
        if method == 'max':
            pool = KernelPooler.max
        elif method == 'ave':
            pool = KernelPooler.ave
        elif method == 'rms':
            pool = KernelPooler.rms
        else:
            # get the pooler and the dimension
            pool = method.pool
            output_dim = method.dim(output_dim)
        image_size = np.asarray(image.shape[:2])
        kernel = self.specs['kernel']
        kernel_size = np.asarray(kernel.shape, dtype=int)
        stride = self.specs['stride']
        grid = ((image_size - kernel_size) / stride).astype(int)
        pool_size = grid * stride + kernel_size
        offset = ((image_size - pool_size) / 2).astype(int)
        output = np.zeros((grid[0], grid[1], output_dim))
        cache = np.zeros((kernel_size[0], kernel_size[1], output_dim))
        cache_2d = cache.view()
        cache_2d.shape = (kernel_size[0] * kernel_size[1], output_dim)
        # for max, rms and average pooling, we have faster methods to do it
        for i in range(grid[0]):
            for j in range(grid[1]):
                topleft = offset + stride * (i,j)
                bottomright = topleft + kernel_size
                cache[:] = image[topleft[0]:bottomright[0], 
                                 topleft[1]:bottomright[1]]
                cache *= kernel[:, :, np.newaxis]
                pool(cache_2d, output[i,j])
        return output
    
    @staticmethod
    def kernel_gaussian(size, sigma):
        """ A Gaussian kernel of the given size and given sigma
        
        Input:
            size: the size of the gaussian kernel. Should be an odd number
            sigma: the standard deviation of the gaussian kernel.
        """
        size = max(size, 3)
        if size % 2 == 0:
            size += 1
        k = (size-1) / 2
        G = - np.arange(-k, k+1)**2
        G = (G + G[:, np.newaxis]) / (2. * sigma * sigma)
        np.exp(G, out = G)
        return G
    
    @staticmethod
    def kernel_uniform(size):
        """ A uniform kernel of the given size
        """
        if type(size) is int:
            size = (size, size)
        G = np.ones(size)
        return G


if __name__ == "__main__":
    print "It works!"
