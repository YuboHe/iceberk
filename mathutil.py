import numpy as np
from iceberk import mpi

def gemm(alpha, A, B, dtype=None, out=None):
    '''A gemm function that uses scipy fblas functions, avoiding matrix copy
    when the input is transposed.
    
    The returned matrix is designed to be C_CONTIGUOUS.
    '''
    from scipy.linalg.fblas import dgemm, sgemm
    if A.ndim != 2 or B.ndim != 2:
        raise TypeError, 'mygemm only deals with 2-D matrices.'
    if dtype is None:
        dtype=A.dtype
    if dtype != np.float32 and dtype != np.float64:
        raise TypeError, 'Error: this function cannot deal with dtype {}.'\
                .format(dtype)
    if not (A.flags['F_CONTIGUOUS'] or A.flags['C_CONTIGUOUS']) \
            or not (B.flags['F_CONTIGUOUS'] or B.flags['C_CONTIGUOUS']):
        raise TypeError, 'Matrices should either be C or F contiguous.'
    if A.dtype != dtype:
        A=np.asarray(A,dtype=dtype)
    if B.dtype != dtype:
        B=np.asarray(B,dtype=dtype)
    
    # In fact, what we are doing here is (1) compute B*A, and (2) transpose the
    # result. The reason is that fblas returns F_CONTINUOUS matrices, so doing 
    # this enables us to get a final output that is C_CONTIGUOUS.
    if not B.flags['F_CONTIGUOUS']:
        B = B.T
        trans_b=0
    else:
        trans_b=1
    if not A.flags['F_CONTIGUOUS']:
        A = A.T
        trans_a=0
    else:
        trans_a=1
        
    # actually do the computation. Instead of doing A*B we actually do (B'*A')'
    # to make the output matrix C-contiguous as we often want.
    if dtype == np.float32:
        fblas_gemm = sgemm
    else:
        fblas_gemm = dgemm
    if out is None:
        return fblas_gemm(alpha,B,A,trans_a=trans_b,trans_b=trans_a).T
    else:
        if out.dtype != dtype:
            raise TypeError, "The output matrix should have type %s"\
                    % repr(dtype)
        if not out.flags['C_CONTIGUOUS']:
            raise TypeError, "The output matrix should be C contiguous."
        fblas_gemm(alpha, B, A, 0.0, out.T, trans_b, trans_a, True)
        return out

def dot(A,B):
    '''
    a simple wrapper that mimics np.dot (if A and B are both matrices!)
    This function solves the problem that np.dot copies matrices when
    working on Matrix.T structures.A
    Input:
        A, B: two matrices. should be either c-contiguous or f-contiguous
    Output:
        out: the output matrix
    Raises:
        TypeError, if the type of matrices is wrong.
    '''
    return gemm(1.0,A,B)

def dot_image(image, B):
    """ A wrapper that does dot for a multidimensional image that is often used
    in the pipeline. The input image should be C-contiguous.
    """
    
    imshape = image.shape
    if not image.flags['C_CONTIGUOUS']:
        raise TypeError, 'Error: cannot deal with non-C-contiguous image'
    output = gemm(1.0, image.reshape((np.prod(imshape[:-1]), imshape[-1])), B)
    return output.reshape(imshape[:-1] + (B.shape[1],))

def exp(X, out = None):
    """ A (hacky) safe exp that avoids overflowing
    """
    if out is None:
        out = np.empty_like(X)
    np.clip(X, -np.inf, 100, out = out)
    # we do in-place exp
    np.exp(out, out = out)
    return out

def log(X, out = None):
    """ A (hacky) safe log that avoids nans
    
    Note that if there are negative values in the input, this function does not
    throw an error. Handle these cases with care.
    """
    if out is None:
        out = np.empty_like(X)
    np.clip(X, np.finfo(np.float64).eps, np.inf, out = out)
    np.log(out, out = out)
    return out

class ReservoirSampler(object):
    """reservoir_sampler implements the reservoir sampling method based on numpy
    matrices. It does NOT use mpi - each mpi node does sampling on its own.
    """
    def __init__(self, num_samples):
        """Initializes the sampler by giving the number of data points N
        """
        self._num_samples = num_samples
        self._current = 0
        self._data = None
        
    def consider(self, feature):
        """Consider a feature batch. feature.shape[1:] should be the same for
        any batch.
        """
        if self._data is None:
            self._data = np.empty((self._num_samples,) + feature.shape[1:],
                                  dtype=feature.dtype)
        elif self._data.shape[1:] != feature.shape[1:]:
            raise ValueError, \
                    "Input data has the wrong size, should be %s " \
                    % str(feature.shape[1:])
        batch_size = feature.shape[0]
        if self._current >= self._num_samples:
            # to make sure we have unbiased sampling, we do 2 steps: first
            # decide whether to use one sample or not, then decide which one it
            # should replace
            # we need to decide if we want to keep the feature
            selected = np.random.rand(batch_size) < \
                    (float(self._num_samples) / np.arange(self._current + 1,
                            self._current + batch_size + 1))
            count = selected.sum()
            self._data[np.random.randint(self._num_samples, size = count)] = \
                    feature[selected]
            self._current += batch_size
        else:
            # we need to fill the data first, and then deal with remaining
            # features
            count = min(self._num_samples - self._current, batch_size)
            self._data[self._current:self._current+count] = feature[:count]
            self._current += count
            if count < batch_size:
                # we have remaining features to consider
                self.consider(feature[count:])
    
    def num_considered(self):
        """Return the number of considered samples
        """
        return self._current
    
    def get(self):
        """After considering all samples, call get() to get the sampled
        features
        """
        if self._current < self._num_samples:
            # not enough data considered. Only return valid ones
            return self._data[:self._current]
        else:
            return self._data

def mpi_mean(data):
    """An mpi implementation of the mean over different nodes.
    """
    s_local = data.sum(0)
    m = np.empty_like(s_local)
    mpi.COMM.Allreduce(s_local, m)
    num_data = mpi.COMM.allreduce(data.shape[0])
    m /= float(num_data)
    return m

def mpi_std(data):
    """An mpi implementation of the std over different nodes.
    """
    m = mpi_mean(data)
    data_centered = data - m
    data_centered **= 2
    std_local = data_centered.sum(0)
    std = np.empty_like(std_local)
    mpi.COMM.Allreduce(std_local, std)
    num_data = mpi.COMM.allreduce(data.shape[0])
    std /= float(num_data)
    return std

def mpi_cov(data):
    """An mpi implementation of the covariance matrix over different nodes
    """
    m = mpi_mean(data)
    data_centered = data - m
    cov_local = dot(data_centered.T, data_centered)
    covmat = np.empty_like(cov_local)
    mpi.COMM.Allreduce(cov_local, covmat)
    num_data = mpi.COMM.allreduce(data.shape[0])
    covmat /= float(num_data)
    return covmat

