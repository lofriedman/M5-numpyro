import logging
logging.basicConfig(level=logging.INFO)
from modules.transform import log_normalise,cluster,expectation_convolution,transform,hump
from modules.numpyro_models import normal_model_hierarchical,poisson_model_hierarchical
from modules.inference import run_inference,posterior_predictive
from modules.metrics import Metrics
from modules.plots import plot_fit,plot_inference,plot_parameter_by_inference,plot_sales_and_covariate
from modules.utils import load_training_data
import jax.numpy as np
from jax import lax, random, vmap
from jax.nn import softmax
import numpy as onp
import numpyro
import pyro
import torch
from pyro.contrib.forecast import ForecastingModel, Forecaster, backtest, eval_crps, HMCForecaster
from modules.pyro_models import *
from pyro.infer.reparam import LocScaleReparam, StableReparam
from pyro.ops.tensor_utils import periodic_cumsum, periodic_repeat, periodic_features
from pyro.ops.stats import quantile
import matplotlib.pyplot as plt
logger = logging.getLogger()

def jax_to_torch(x):
    data_ = onp.asarray(x)
    return torch.from_numpy(data_)

def load_input():
    assert numpyro.__version__.startswith('0.2.4')
    numpyro.set_host_device_count(4)
    logger.info('Main starting')
    steps = 2
    n_days = 15
    items = range(0,150)
    variable = ['sales']  # Target variables
    covariates = ['month', 'snap', 'christmas', 'event', 'price', 'trend']  # List of considered covariates
    ind_covariates = ['price', 'snap']  # Item-specific covariates
    common_covariates = set(covariates).difference(ind_covariates)  # List of non item-specific covariates
    t_covariates = ['event', 'christmas']  # List of transformed covariates
    norm_covariates = ['price','trend']  # List of normalised covariates
    hump_covariates = ['month']  # List of convoluted covariates
    logger.info('Loading data')
    calendar, training_data = load_training_data(items=items, covariates=covariates)
    training_data = transform(expectation_convolution, training_data, t_covariates, steps, False)
    training_data = transform(log_normalise, training_data, norm_covariates)
    training_data = transform(hump, training_data, hump_covariates, n_days)
    y = np.array(training_data[variable[0]])
    X_i = np.stack([training_data[x] for x in ind_covariates], axis=1)
    X_i_dim = dict(zip(ind_covariates, [1 for x in ind_covariates]))
    X_c = np.repeat(np.hstack([training_data[i] for i in common_covariates])[..., np.newaxis],
                    repeats=len(items),
                    axis=2)
    X_c_dim = dict(zip(common_covariates, [training_data[x].shape[-1] for x in common_covariates]))
    X = np.concatenate([X_c, X_i], axis=1)
    # Aggregation
    y,X,clusters = cluster(y,X,10)
    X_dim = {**X_c_dim, **X_i_dim}
    return {'X': X,
            'X_dim': X_dim,
            'y': y},calendar

def main(pyro_backend=None):
    inputs,calendar = load_input()
    logger.info('Inference')
    if pyro_backend is None:
        samples_hmc = run_inference(model=normal_model_hierarchical,inputs=inputs)
        inputs.pop('y')
        trace = posterior_predictive(normal_model_hierarchical, samples_hmc, inputs)
        metric_data = {'trace':trace,
                       'actual':y,
                       'alpha':0.95}
        m = Metrics(**metric_data)
        forecasts = m.moments
        hit_rate = m.hit_rate
        plot_fit(forecasts,hit_rate, y, calendar)
        print(r'Hit rate={0:0.2f}'.format(hit_rate))
    else:
        covariates, covariate_dim, data = inputs.values()
        data, covariates = map(jax_to_torch,[data,covariates])
        data = torch.log(1+data.double())
        assert pyro.__version__.startswith('1.3.1')
        pyro.enable_validation(True)
        T0 = 0  # begining
        T2 = data.size(-2)  # end
        T1 = T2 - 1000  # train/test split
        pyro.set_rng_seed(1)
        pyro.clear_param_store()
        data = data.permute(-2,-1)
        covariates = covariates.reshape(data.size(-1),T2,-1)
        # covariates = torch.zeros(len(data), 0)  # empty
        forecaster = Forecaster(Model4(), data[:T1], covariates[:,:T1], learning_rate=0.05,num_steps=2000)
        samples = forecaster(data[:T1], covariates[:,:T2], num_samples=336)
        samples.clamp_(min=0)  # apply domain knowledge: the samples must be positive
        p10, p50, p90 = quantile(samples[:, 0], [0.1, 0.5, 0.9]).squeeze(-1)
        crps = eval_crps(samples, data[T1:T2])
        print(samples.shape, p10.shape)
        fig, axes = plt.subplots(data.size(-1), 1, figsize=(9, 10), sharex=True)
        plt.subplots_adjust(hspace=0)
        axes[0].set_title("Sales (CRPS = {:0.3g})".format(crps))
        for i, ax in enumerate(axes):
            ax.fill_between(torch.arange(T1, T2), p10[:, i], p90[:, i], color="red", alpha=0.3)
            ax.plot(torch.arange(T1, T2), p50[:, i], 'r-', lw=1, label='forecast')
            ax.plot(torch.arange(T0, T2),data[: T2, i], 'k-', lw=1, label='truth')
            ax.set_ylabel(f"item: {i}")
        axes[0].legend(loc="best")
        plt.show()
        plt.savefig('figures/pyro_forecast.png')



if __name__ == '__main__':
    main(pyro_backend=True)