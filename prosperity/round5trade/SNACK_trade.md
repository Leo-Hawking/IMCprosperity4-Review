对于所有SNACK五个产品，被动挂单逻辑和之前的产品一致

主动挂单：
对于所有主动挂单，均只take价格等于fair price的订单，绝不跨越fair
CHOCOLATE and VANILLA:take目标为使得二者头寸尽量相等并且绝对值尽量小

STRAWBERRY：能买就买，多多益善
PISTACHIO：能卖就卖，多多益善
RASPBERRY: fair price大于miu+threshold则买入，小于则卖出

miu初始值为10000，采用超长周期ema（span = 4000），threshold暂定为200