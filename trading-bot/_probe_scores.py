from bot.data.fetcher import get_exchange, fetch_multi_timeframe
from bot.strategy import scorer

sym='BTC/USDT'
ex=get_exchange(paper_mode=True)
dfs=fetch_multi_timeframe(ex,sym,['1h','4h','1d'])
df1=dfs['1h']; df4=dfs['4h']; dfd=dfs['1d']

for test_min in [68,58,50,45,40,35,30,25,20]:
    scorer.MIN_SIGNAL_SCORE=test_min
    sent=0; non_neutral=0; checked=0
    max_i=min(len(df1)-1,900)
    for i in range(120,max_i):
        ts=df1.index[i]
        s=scorer.score_signal(df1.iloc[:i+1], df4[df4.index<=ts], sym, df_daily=dfd[dfd.index<=ts])
        checked+=1
        if s.get('direction') in {'long','short'}:
            non_neutral+=1
        if s.get('send_alert'):
            sent+=1
    print({'min':test_min,'checked':checked,'non_neutral':non_neutral,'send_alert':sent})
