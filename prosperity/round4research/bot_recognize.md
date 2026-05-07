针对bot行为的分析方案
数据路径：/Users/leoliu/imc/prosperity/data/bt
在round4的trade中披露了成交双方的bot编号，也就是每个bot都有完整的成交记录。为了分析每个bot的行为逻辑，我希望针对每个bot的成交记录进行可视化，可视化方法可参考/Users/leoliu/imc/prosperity/backtest/review.ipynb（不要照搬，可简化）。你需要新生成一个ipynb文件，针对每一个bot标记出它在市场上的交易位置，并且该市场图与下方仓位曲线与pnl曲线共享横轴。

还要做一个pnl分解图，针对每一个pnl分解成交易pnl和持仓pnl。
此外你还可以考虑添加一些可能有助于分析bot行为的功能。


市场价格计算：wall mid，最大订单量的订单价格相加除以二，若单边缺失则前向填充。该价格可作为除了主动被动成交判断外的任何价格相关逻辑。
主动被动成交判断还需要对比最优买价和卖价的一半（该ipynb中可能用不上）