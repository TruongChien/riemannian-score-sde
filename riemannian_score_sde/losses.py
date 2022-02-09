"""All functions related to loss computation and optimization.
"""

from typing import Callable, Tuple

import jax
import jax.numpy as jnp
import jax.random as random

from score_sde.utils import batch_mul
from score_sde.models import get_score_fn, PushForward, SDEPushForward, MoserFlow
from score_sde.utils import ParametrisedScoreFunction, TrainState
from score_sde.models import div_noise, get_div_fn, get_ode_drift_fn


def get_dsm_loss_fn(
    pushforward: SDEPushForward,
    model: ParametrisedScoreFunction,
    train: bool = True,
    reduce_mean: bool = True,
    like_w: bool = True,
    eps: float = 1e-3,
    s_zero = True,
    **kwargs
):
    sde = pushforward.sde
    reduce_op = (
        jnp.mean
        if reduce_mean
        else lambda *args, **kwargs: 0.5 * jnp.sum(*args, **kwargs)
    )

    def loss_fn(
        rng: jax.random.KeyArray, params: dict, states: dict, batch: dict
    ) -> Tuple[float, dict]:
        score_fn = get_score_fn(
            sde,
            model,
            params,
            states,
            train=train,
            return_state=True,
        )
        x_0 = batch["data"]

        rng, step_rng = random.split(rng)
        # uniformly sample from SDE timeframe
        t = random.uniform(
            step_rng, (x_0.shape[0],), minval=sde.t0 + eps, maxval=sde.tf
        )
        rng, step_rng = random.split(rng)

        # sample p(x_t | x_0)
        # compute $\nabla \log p(x_t | x_0)$
        if s_zero:  # l_{t|0}
            x_t = sde.marginal_sample(step_rng, x_0, t)
            logp_grad = sde.grad_marginal_log_prob(x_0, x_t, t, **kwargs)[1]
            std = jnp.expand_dims(sde.marginal_prob(jnp.zeros_like(x_t), t)[1], -1)
        else:      # l_{t|s}
            x_t, x_hist, timesteps = sde.marginal_sample(step_rng, x_0, t, return_hist=True)
            x_s = x_hist[-2]
            logp_grad, delta_t = sde.varhadan_exp(x_s, x_t, timesteps[-2], timesteps[-1])
            delta_t = t  # NOTE: works better?
            std = jnp.expand_dims(sde.marginal_prob(jnp.zeros_like(x_t), delta_t)[1], -1)

        # compute approximate score at x_t
        score, new_model_state = score_fn(x_t, t, rng=step_rng)

        if not like_w:
            score = batch_mul(std, score)
            logp_grad = batch_mul(std, logp_grad)
            losses = jnp.square(score - logp_grad)
            losses = reduce_op(losses.reshape((losses.shape[0], -1)), axis=-1)

        else:
            # compute $E_{p{x_0}}[|| s_\theta(x_t, t) - \nabla \log p(x_t | x_0)||^2]$
            g2 = sde.coefficients(jnp.zeros_like(x_0), t)[1] ** 2
            losses = jnp.square(score - logp_grad)
            losses = reduce_op(losses.reshape((losses.shape[0], -1)), axis=-1)  * g2

        loss = jnp.mean(losses)
        return loss, new_model_state

    return loss_fn


def get_ism_loss_fn(
    pushforward: SDEPushForward,
    model: ParametrisedScoreFunction,
    train: bool,
    reduce_mean: bool = True,
    like_w: bool = True,
    hutchinson_type="Rademacher",
    eps: float = 1e-3,
):
    sde = pushforward.sde

    def loss_fn(
        rng: jax.random.KeyArray, params: dict, states: dict, batch: dict
    ) -> Tuple[float, dict]:
        score_fn = get_score_fn(
            sde,
            model,
            params,
            states,
            train=train,
            return_state=True,
        )
        x_0 = batch["data"]

        rng, step_rng = random.split(rng)
        t = random.uniform(
            step_rng, (x_0.shape[0],), minval=sde.t0 + eps, maxval=sde.tf
        )

        rng, step_rng = random.split(rng)
        x_t = sde.marginal_sample(step_rng, x_0, t)
        score, new_model_state = score_fn(x_t, t, rng=step_rng)

        # ISM loss
        rng, step_rng = random.split(rng)
        epsilon = div_noise(step_rng, x_0.shape, hutchinson_type)
        drift_fn = lambda x, t: score_fn(x, t, rng=step_rng)[0]
        div_fn = get_div_fn(drift_fn, hutchinson_type)
        div_score = div_fn(x_t, t, epsilon)
        sq_norm_score = sde.manifold.metric.squared_norm(score, x_t)
        losses = 0.5 * sq_norm_score + div_score

        if like_w:
            g2 = sde.coefficients(jnp.zeros_like(x_0), t)[1] ** 2
            losses = losses * g2

        loss = jnp.mean(losses)
        return loss, new_model_state

    return loss_fn


def get_moser_loss_fn(
    pushforward: MoserFlow,
    model: ParametrisedScoreFunction,
    alpha_m: float,
    alpha_p: float,
    K: int,
    hutchinson_type: str,
    eps: float,
    **kwargs
):
    def loss_fn(
        rng: jax.random.KeyArray, params: dict, states: dict, batch: dict
    ) -> Tuple[float, dict]:
        drift_fn = get_ode_drift_fn(model, params, states)
        x_0 = batch["data"]

        rng, step_rng = random.split(rng)
        div_fn = get_div_fn(drift_fn, hutchinson_type)

        def mu(x):
            prob_base = jnp.exp(pushforward.base.log_prob(x))
            # x = x.reshape(-1, *x.shape)
            t = jnp.zeros((*x.shape[:-1],))  # NOTE: How to deal with that?
            epsilon = div_noise(step_rng, x.shape, hutchinson_type)
            div_drift = div_fn(x, t, epsilon)
            # div_drift = jnp.squeeze(div_drift)
            mu = prob_base - div_drift
            mu_plus = jnp.maximum(eps, mu)
            mu_minus = eps - jnp.minimum(eps, mu)
            return mu_plus, mu_minus
        # mu = jax.vmap(mu)  #NOTE: gives different results?

        mu_plus = mu(x_0)[0]
        log_prob = jnp.mean(jnp.log(mu_plus))

        rng, step_rng = random.split(rng)
        xs = pushforward.base.sample(step_rng, (K,))
        prior_prob = jnp.exp(pushforward.base.log_prob(xs))

        _, mu_minus = mu(xs)
        volume_m = jnp.mean(batch_mul(mu_minus, 1 / prior_prob) , axis=0)
        penalty = alpha_m * volume_m# + alpha_p * volume_p

        loss = - log_prob + penalty

        # return loss, new_model_state
        return loss, states

    return loss_fn
