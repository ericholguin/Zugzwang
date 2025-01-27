import os
import time
import math
import h5py
import numpy
import model
import random
import pickle
import theano
import itertools
import preprocess
import scipy.sparse
import theano.tensor as T
from theano.tensor.nnet import sigmoid
from sklearn.model_selection import train_test_split

data_path = 'Data/600-799'
pickle_name = '600-799model.pickle'
MINIBATCH_SIZE = 1000


def floatX(x):
    """
    Returns theano float value
    """
    return numpy.asarray(x, dtype=theano.config.floatX)

def load_data(data_path):
    """
    Loading hdf5 files from directory
    """
    for filename in os.listdir(data_path):
        if not filename.endswith('.hdf5'):
            continue

        infile = os.path.join(data_path, filename)
        try:
            yield h5py.File(infile, 'r')
        except:
            print("Could not read: {}".format(infile))   
            

def get_data(series=['x', 'xr']):
    """
    Extract data from files
    Call to load data function
    """
    data = [[] for s in series]
    for f in load_data(data_path):
        try:
            for i, s in enumerate(series):
                data[i].append(f[s].value)
        except:
            raise
            print("Failed reading from: {}".format(f)) 

    def stack(vectors):
        if len(vectors[0].shape) > 1:
            return numpy.vstack(vectors)
        else:
            return numpy.hstack(vectors)

    data = [stack(d) for d in data]

    test_size = 1 / len(data[0])

    print ("Splitting: {} entries into train/test set".format(len(data[0]))) 

    data = train_test_split(*data, test_size=test_size)

    print("Train set:")
    print(data[0].shape[0]) 
    print("Test set:") 
    print(data[1].shape[0])

    return data



def get_training_model(Ws_s, bs_s, dropout=False, lambd=10.0, kappa=1.0):
    # Build a dual network, one for the real move, one for a fake random move
    # Train on a negative log likelihood of classifying the right move

    xc_s, xc_p = model.get_model(Ws_s, bs_s, dropout=dropout)
    xr_s, xr_p = model.get_model(Ws_s, bs_s, dropout=dropout)
    xp_s, xp_p = model.get_model(Ws_s, bs_s, dropout=dropout)

    #loss = -T.log(sigmoid(xc_p + xp_p)).mean() # negative log likelihood
    #loss += -T.log(sigmoid(-xp_p - xr_p)).mean() # negative log likelihood

    cr_diff = xc_p - xr_p
    loss_a = -T.log(sigmoid(cr_diff)).mean()

    cp_diff = kappa * (xc_p + xp_p)
    loss_b = -T.log(sigmoid( cp_diff)).mean()
    loss_c = -T.log(sigmoid(-cp_diff)).mean()

    # Add regularization terms
    reg = 0
    for x in Ws_s + bs_s:
        reg += lambd * (x ** 2).mean()

    loss = loss_a + loss_b + loss_c
    return xc_s, xr_s, xp_s, loss, reg, loss_a, loss_b, loss_c


def nesterov_updates(loss, all_params, learn_rate, momentum):
    updates = []
    all_grads = T.grad(loss, all_params)
    for param_i, grad_i in zip(all_params, all_grads):
        # generate a momentum parameter
        mparam_i = theano.shared(
            numpy.array(param_i.get_value()*0., dtype=theano.config.floatX))
        v = momentum * mparam_i - learn_rate * grad_i
        w = param_i + momentum * v - learn_rate * grad_i
        updates.append((param_i, w))
        updates.append((mparam_i, v))
    return updates


def get_function(Ws_s, bs_s, dropout=False, update=False):
    xc_s, xr_s, xp_s, loss_f, reg_f, loss_a_f, loss_b_f, loss_c_f = get_training_model(Ws_s, bs_s, dropout=dropout)
    obj_f = loss_f + reg_f

    learning_rate = T.scalar(dtype=theano.config.floatX)

    momentum = floatX(0.9)

    if update:
        updates = nesterov_updates(obj_f, Ws_s + bs_s, learning_rate, momentum)
    else:
        updates = []

    print("Compiling function")
    f = theano.function(
        inputs=[xc_s, xr_s, xp_s, learning_rate],
        outputs=[loss_f, reg_f, loss_a_f, loss_b_f, loss_c_f],
        updates=updates,
        on_unused_input='warn')

    return f

def train():
    Xc_train, Xc_test, Xr_train, Xr_test, Xp_train, Xp_test = get_data(['x', 'xr', 'xp'])
    for board in [Xc_train[0], Xp_train[0]]:
        for row in range(8):
            print(' '.join('%2d' % x for x in board[(row*8):((row+1)*8)]))

    n_in = 12 * 64

    Ws_s, bs_s = model.get_parameters(n_in=n_in, n_hidden_units=[2048] * 3)
    
    minibatch_size = min(MINIBATCH_SIZE, Xc_train.shape[0])

    train = get_function(Ws_s, bs_s, update=True, dropout=False)
    test = get_function(Ws_s, bs_s, update=False, dropout=False)

    best_test_loss = float('inf')
    base_learning_rate = 0.02
    t0 = time.time()
    
    i = 0
    while True:
        i += 1
        learning_rate = floatX(base_learning_rate * math.exp(-(time.time() - t0) / 86400))

        minibatch_index = random.randint(0, int(Xc_train.shape[0] / minibatch_size) - 1)
        lo, hi = minibatch_index * minibatch_size, (minibatch_index + 1) * minibatch_size
        loss, reg, loss_a, loss_b, loss_c = train(Xc_train[lo:hi], Xr_train[lo:hi], Xp_train[lo:hi], learning_rate)

        zs = [loss, loss_a, loss_b, loss_c, reg]
        print("Iteration %6d learning rate %12.9f: %s" % (i, learning_rate, '\t'.join(['%12.9f' % z for z in zs])))

        if i % 200 == 0:
            test_loss, test_reg, _, _, _ = test(Xc_test, Xr_test, Xp_test, learning_rate)
            print("test loss %12.9f" % test_loss)

            if test_loss < best_test_loss:
                print("new record!")
                best_test_loss = test_loss

                print("dumping pickled model")
                f = open(pickle_name, 'wb')
                def values(zs):
                    return [z.get_value(borrow=True) for z in zs]
                pickle.dump((values(Ws_s), values(bs_s)), f)
                f.close()


if __name__ == '__main__':
    train()
