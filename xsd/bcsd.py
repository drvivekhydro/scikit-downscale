#!/usr/bin/env python
''' Bias Correction and Statistical Downscaling (BCSD) method'''

# 1. set list of models to use -- will these be the same 97 as in the CONUS
# dataset?  if using new ones, need to make sure that there are both
# retrospective and projection runs for the same model. (edited)
#
# 2.  regrid those model projections (pr, tas, tasmin and tasmax) to common
# (1 degree)? grid -- can also clip to a desired domain in process (edited)
#
# 3. from historical climate model runs, calculate climo PDFs by model, month
# and cell.  We can handle extrapolation by fitting distributions (ie normal
# for temperature; EV1 or EV3 for precip, fit to the upper half of the
# distribution.
#
# 4.  upscale obs historical data to the GCM grid.  Depending on the period of
# record available, this will control the POR of the bias-correction.
#
# 5.  calculate climo PDFs for obs data with extrapolation distrib. fits as in
# step 3.
#
# 6.  BC projection precip at the model grid scale, then calculate
# multiplicative anomalies relative to the historical climo mean.  Finally
# interpolate anomalies to target forcing resolution.
#
# 7.  calculate running mean temperature increase for model projections, save,
# and subtract from model projections.
#
# 8.  BC projection temperatures after mean shift removal, then add back the
# mean shift.  Calculate additive anomalies relative to historical climo mean
# and interpolate to target forcing resolution.
#
# And that's it.  From there the other other scripts could handle the daily
# disag to get the final forcings.  I probably should have stuck that all in a
# 1 pager, sorry -- mainly I just though it would be good to be clear on the
# steps if we're thinking of going for gold with xarray.

import numpy as np
import xarray as xr
import xesmf as xe

from .quantile_mapping import quantile_mapping_by_group


def get_bounds(obj, lat_var='lat', lon_var='lon'):
    ''' Determine the latitude and longitude bounds of a xarray object'''
    return {'lat': (obj[lat_var].values.min(), obj[lat_var].values.max()),
            'lon': (obj[lon_var].values.min(), obj[lon_var].values.max())}


def _make_source_grid(obj):
    ''' Add longitude and latitude bounds to an xarray object

    Note
    ----
    This function is only valid if the object is already on a regular lat/lon
    grid.
    '''
    lon_step = np.diff(obj.lon.values[:2])[0]
    lat_step = np.diff(obj.lat.values[:2])[0]

    obj.coords['lon_b'] = ('x_b', np.append(obj.lon.values - 0.5*lon_step,
                           obj.lon.values[-1] + 0.5*lon_step))
    obj.coords['lat_b'] = ('y_b', np.append(obj.lat.values - 0.5*lat_step,
                           obj.lat.values[-1] + 0.5*lat_step))
    return obj


def _running_mean(obj, **kwargs):
    '''helper function to apply rolling mean to groupby object'''
    return obj.rolling(**kwargs).mean()


def _regrid_to(dest, method='bilinear', *objs):
    ''' helper function to handle regridding a batch of objects to a common
    grid
    '''
    out = []
    for obj in objs:
        obj = _make_source_grid(obj)  # add grid info if needed
        regridder = xe.Regridder(obj, dest, method)  # construct the regridder
        out.append(regridder(obj))  # do the regrid op
    return out


def bcsd(da_obs, da_train, da_predict, var='pr'):
    ''' Apply the Bias Correction and Statistical Downscaling (BCSD) method.

    Parameters
    ----------
    da_obs : xr.DataArray
        Array representing the observed (truth) values.
    da_train : xr.DataArray
        Array representing the training data.
    da_predict : xr.DataArray
        Array representing the prediction data to be corrected using the BCSD
        method.
    var : str
        Variable name triggering particular treatment of some variables. Valid
        options include {'pr', 'tmin', 'tmax', 'trange', 'tavg'}.

    Returns
    -------
    out_regrid : xr.DataArray
        Anomalies on the same grid as ``da_obs``.
    '''

    # regrid to common course grid
    bounds = get_bounds(da_obs)
    course_grid = xe.util.grid_2d(*bounds['lon'], 1, *bounds['lat'], 1)
    da_obs_regrid, da_train_regrid, da_predict_regrid = _regrid_to(
        course_grid, da_obs, da_train, da_predict)

    # Calc mean climatology for training data
    da_train_regrid_mean = da_train_regrid.groupby(
        'time.month').mean(dim='time')

    if var == 'pr':
        # Bias correction
        # apply quantile mapping by month
        da_predict_regrid_qm = quantile_mapping_by_group(
            da_predict_regrid, da_train_regrid, da_obs_regrid,
            grouper='time.month')

        # calculate the amonalies as a ratio of the training data
        # again, this is done month-by-month
        da_predict_regrid_anoms = (da_predict_regrid_qm.groupby('time.month')
                                   / da_train_regrid_mean)
    else:
        # Calculate the 9-year running mean for each month
        # Q: don't do this for training period? check with andy
        da_predict_regrid_rolling_mean = da_predict_regrid.groupby(
            'time.month').apply(_running_mean, time=9, center=True,
                                min_periods=1)

        # Calculate the anomalies relative to each 9-year window
        da_predict_anoms = da_predict_regrid - da_predict_regrid_rolling_mean

        # Bias correction
        # apply quantile mapping by month
        da_predict_regrid_qm = quantile_mapping_by_group(
            da_predict_regrid, da_train_regrid, da_obs_regrid,
            grouper='time.month')

        # calc anoms (difference)
        # this is obviously not what we want
        da_predict_regrid_qm_mean = (da_predict_regrid_rolling_mean
                                     + da_predict_regrid_qm)

        # this is obviously not what we want
        da_predict_regrid_anoms = (da_predict_regrid_qm -
                                   da_predict_regrid_qm_mean)

    # regrid to obs grid
    out_regrid, = _regrid_to(da_obs, da_predict_regrid_anoms,
                             method='bilinear')

    # return regridded anomalies
    return out_regrid


def main():

    obs_fname = '/glade/u/home/jhamman/workdir/GARD_inputs/newman_ensemble/conus_ens_004.nc'
    train_fname = '/glade/p/ral/RHAP/gutmann/cmip/daily/CNRM-CERFACS/CNRM-CM5/historical/day/atmos/day/r1i1p1/latest/pr/*nc'
    predict_fname = '/glade/p/ral/RHAP/gutmann/cmip/daily/CNRM-CERFACS/CNRM-CM5/rcp45/day/atmos/day/r1i1p1/latest/pr/*nc'

    out = xr.Dataset()
    for var in ['pr']:

        # get variables from the obs/training/prediction datasets
        da_obs_daily = xr.open_mfdataset(obs_fname)[var]
        da_obs = da_obs_daily.resample(time='MS').mean('time').load()
        da_train = xr.open_mfdataset(train_fname)[var].resample(
            time='MS').mean('time').load()
        da_predict = xr.open_mfdataset(predict_fname)[var].resample(
            time='MS').mean('time').load()

        out[var] = bcsd(da_obs, da_train, da_predict, var=var)

    out_file = './test.nc'
    print('writing outfile %s' % out_file)
    out.to_netcdf(out_file)


if __name__ == '__main__':
    main()