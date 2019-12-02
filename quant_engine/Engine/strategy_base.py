import pandas as pd
import numpy as np
from rdf_data import rdf_data
from influxdb_data import influxdbData
from data_process import DataProcess
from joblib import Parallel,delayed,parallel_backend
import warnings
import statsmodels.api as sm

class StrategyBase:
    @staticmethod
    def cross_section_remove_outlier(factor_data,factor_field,dates):
        res = []
        for date in dates:
            day_factor = factor_data.loc[date,:].copy()
            day_factor.loc[:,factor_field] = DataProcess.remove_outlier(day_factor[factor_field])
            res.append(day_factor)
        dates_factor = pd.concat(res)
        return dates_factor


    @staticmethod
    def cross_section_Z_standardize(factor_data,factor_field,dates):
        res = []
        for date in dates:
            day_factor = factor_data.loc[date, :].copy()
            day_factor.loc[:,factor_field] = DataProcess.Z_standardize(day_factor[factor_field])
            res.append(day_factor)
        dates_factor = pd.concat(res)
        return dates_factor


    @staticmethod
    def cross_section_rank_standardize(factor_data,factor_field,dates):
        res = []
        for date in dates:
            day_factor = factor_data.loc[date, :].copy()
            day_factor.loc[:,factor_field] = DataProcess.rank_standardize(day_factor[factor_field])
            res.append(day_factor)
        dates_factor = pd.concat(res)
        return dates_factor


    @staticmethod
    def regression(mkt_data,factor_field,dates):
        RLM_res = []
        filtered_factor_res = []
        idsty_size_cols = mkt_data.columns.difference(['code','date','former_trade_day','next_trade_day','return',factor_field])
        for date in dates:
            day_code = mkt_data.loc[mkt_data['date']==date,'code']
            day_factor = mkt_data.loc[mkt_data['date']==date,factor_field]
            day_return = mkt_data.loc[mkt_data['date']==date,'return']
            day_idsty_size = mkt_data.loc[mkt_data['date']==date,idsty_size_cols]
            OLS_est = sm.OLS(day_factor, day_idsty_size).fit()
            day_filtered_factor = OLS_est.resid
            day_filtered_factor.name = factor_field
            RLM_est = sm.RLM(day_return, day_filtered_factor, M=sm.robust.norms.HuberT()).fit()
            day_RLM_para = RLM_est.params
            day_Tvalue = RLM_est.tvalues
            # 得到正交化后的因子值
            day_filtered_factor = pd.concat([day_code,day_filtered_factor],axis=1)
            day_filtered_factor['date'] = date
            day_RLM_result = pd.DataFrame({'Fvalue':day_RLM_para.iloc[0],'Tvalue':day_Tvalue.iloc[0]},index=[date])
            RLM_res.append(day_RLM_result)
            filtered_factor_res.append(day_filtered_factor)
        RLM_result = pd.concat(RLM_res)
        filtered_factor = pd.concat(filtered_factor_res)
        return {'RLM_result':RLM_result,'filtered_factor':filtered_factor}


    def __init__(self):
        self.rdf = rdf_data()
        self.influx = influxdbData()


    def get_former_trade_day(self,mkt_data):
        calendar = self.rdf.get_trading_calendar()
        trade_day = pd.DataFrame(mkt_data.index.unique().tolist(), columns=['date'])
        trade_day['former_trade_day'] = trade_day['date'].apply(lambda x: calendar[calendar < x].iloc[-1])
        trade_day.set_index('date', inplace=True)
        return trade_day


    def get_next_trade_day(self,mkt_data):
        calendar = self.rdf.get_trading_calendar()
        trade_day = pd.DataFrame(mkt_data.index.unique().tolist(), columns=['date'])
        trade_day['next_trade_day'] = trade_day['date'].apply(lambda x: calendar[calendar > x].iloc[0])
        trade_day.set_index('date', inplace=True)
        return trade_day


    def get_test_info(self,mkt_data,filter_st=True,industry='citics_lv1_name',mkt_cap_field='ln_market_cap'):
        # 过滤停牌(停牌没有收益率)
        mkt_data = mkt_data.loc[(mkt_data['status'] != '停牌') & (pd.notnull(mkt_data['status'])), :]

        # 过滤st
        if filter_st:
            mkt_data = mkt_data.loc[mkt_data['isST']==False,:]
        mkt_data['return'] = mkt_data['close']/mkt_data['preclose'] -1
        # 超过0.11或-0.11的return标记为异常数据，置为nan(新股本身剔除)
        mkt_data = mkt_data.loc[(mkt_data['return']<0.11) | (mkt_data['return']>-0.11),:]
        # 计算former date 和 next date
        mkt_data = pd.merge(mkt_data,self.get_former_trade_day(mkt_data),left_index=True,right_index=True,how='left')
        mkt_data = pd.merge(mkt_data,self.get_next_trade_day(mkt_data),left_index=True,right_index=True,how='left')

        mkt_data.set_index([mkt_data.index,'code'],inplace=True)
        mkt_data.index.names = ['date','code']
        rtn_data = mkt_data.loc[:,['former_trade_day','next_trade_day','return']]
        industry_data = pd.get_dummies(mkt_data[industry])
        # 过滤掉没有行业信息的数据
        industry_data = industry_data.loc[~(industry_data==0).all(axis=1),:]
        rtn_data.reset_index(inplace=True)
        industry_data.reset_index(inplace=True)
        mkt_data = pd.merge(rtn_data,industry_data,how='right',on=['date','code'])

        size_data = self.influx.getDataMultiprocess('DailyFactor_Gus', 'Size',start,end,None)
        size_data.index.names = ['date']
        size_data.reset_index(inplace=True)
        size_data = size_data.loc[:,['date','code',mkt_cap_field]]
        size_data.columns = ['date','code','size']
        # mkt cap 标准化
        size_data['size'] = DataProcess.Z_standardize(size_data['size'])
        mkt_data = pd.merge(mkt_data,size_data,how='inner',on=['date','code'])
        print('test info loaded!')
        return mkt_data


    # factor.index 是date
    # mkt_data 的date在columns里
    def test_factor(self,factor_data,factor_field,test_info,standardize='z',remove_outlier=True):
        # 数据预处理
        dates = factor_data.index.unique()
        split_dates = np.array_split(dates, 30)
        if remove_outlier:
            with parallel_backend('multiprocessing', n_jobs=-1):
                parallel_res = Parallel()(delayed(StrategyBase.cross_section_remove_outlier)
                                          (factor_data, factor_field,dates) for dates in split_dates)
            factor_data = pd.concat(parallel_res)
            print('outlier remove finish!')
        if standardize == 'z':
            with parallel_backend('multiprocessing', n_jobs=-1):
                parallel_res = Parallel()(delayed(StrategyBase.cross_section_Z_standardize)
                                          (factor_data, factor_field,dates) for dates in split_dates)
            factor_data = pd.concat(parallel_res)
            print('Z_standardize finish!')
        elif standardize == 'rank':
            with parallel_backend('multiprocessing', n_jobs=-1):
                parallel_res = Parallel()(delayed(StrategyBase.cross_section_rank_standardize)
                                          (factor_data, factor_field,dates) for dates in split_dates)
            factor_data = pd.concat(parallel_res)
            print('rank_standardize finish!')
        else:
            pass

        factor_data.index.names = ['date']
        factor_data.reset_index(inplace=True)
        test_info = pd.merge(test_info,factor_data,on=['date','code'])

        # 去除行业和市值，得到新因子
        dates = test_info['date'].unique()
        split_dates = np.array_split(dates,30)
        with parallel_backend('multiprocessing', n_jobs=-1):
            parallel_res = Parallel()(delayed(StrategyBase.regression)
                                      (test_info, factor_field, dates) for dates in split_dates)
        print('regression process finish!')
        RLM_res = []
        filtered_factor_res = []
        for r in parallel_res:
            RLM_res.append(r['RLM_result'])
            filtered_factor_res.append(r['filtered_factor'])
        RLM_result = pd.concat(RLM_res)
        F_over_0_pct = RLM_result.loc[RLM_result['Fvalue']>0,:].shape[0] / RLM_result.shape[0]
        avg_abs_T = abs(RLM_result['Tvalue']).sum() / RLM_result.shape[0]
        abs_T_over_2_pct = RLM_result.loc[abs(RLM_result['Tvalue'])>=2,:].shape[0] / RLM_result.shape[0]
        print('-'*30)
        print('REGRESSION RESULT: \n   F_over_0_pct: %f \n   avg_abs_T: %f \n   abs_T_over_2_pct: %f \n' %
              (F_over_0_pct,avg_abs_T,abs_T_over_2_pct))
        filtered_factor = pd.concat(filtered_factor_res)
        return filtered_factor
        # 后续需添加因子收益率输出
        # 后续需添加因子ic值计算

    def group_backtest(self,factor,mkt_data,start,end,groups=5,benchmark='IC',industry_field='citics_lv1_name'):
        benchmark_field = benchmark+'_weight'
        mkt_data = mkt_data.loc[:,[benchmark_field,industry_field,'code','status']]
        mkt_data = pd.merge(mkt_data,self.get_next_trade_day(mkt_data),right_index=True,left_index=True,how='left')
        nxt_day_status = mkt_data.loc[:,['code','status']].copy()
        nxt_day_status.reset_index(inplace=True)
        nxt_day_status.rename(columns={'index':'next_trade_day','status':'next_day_status'},inplace=True)
        mkt_data.reset_index(inplace=True)
        mkt_data.rename(columns={'index':'date'},inplace=True)
        mkt_data = pd.merge(mkt_data,nxt_day_status,right_on=['next_trade_day','code'],left_on=['next_trade_day','code'],how='left')
        print('.')




if __name__ == '__main__':
    warnings.filterwarnings("ignore")
    strategy = StrategyBase()
    start = 20130101
    end = 20160101
    mkt_data = strategy.influx.getDataMultiprocess('DailyData_Gus', 'marketData', start, end, None)
    mkt_data = mkt_data.tz_convert(None)
    test_info = strategy.get_test_info(mkt_data)
    ep_cut = strategy.influx.getDataMultiprocess('DailyFactor_Gus','Value',start,end,['code','EPcut_TTM'])
    ep_cut = ep_cut.dropna(subset=['EPcut_TTM'])
    print('epcut loaded!')
    filtered_factor = strategy.test_factor(ep_cut,'EPcut_TTM',test_info,standardize='z',remove_outlier=False)
    strategy.group_backtest(filtered_factor,mkt_data,20130101,20151231)