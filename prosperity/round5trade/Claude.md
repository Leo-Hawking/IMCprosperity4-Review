round5资产 fair price计算：
首先计算wall mid：最大订单量的买卖单相加除以二，若单边或双边缺失则前向填充订单。
当不存在或存在多个距离wall mid小于1的挂单，则fair price = wall_mid-0.5
当存在且仅存在一个距离wall mid小于1的挂单出现，则该挂单的价格就是fair price
