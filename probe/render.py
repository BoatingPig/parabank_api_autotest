from probe.config import ENDPOINTS
from probe.values import VALUES
from urllib.parse import quote


def render(path, params):
    for k, v in params.items():
        # 把url中占位符替换成传入的value，quote为了给value字符编码防止破坏url
        path = path.replace("{" + k + "}", quote(str(v), safe=""))
    return path


# param是需要放坏值进去的参数，其他参数均由baseline值传入
def param_cases(endpoint, param):
    others = {}
    # 这里的构造非baseline的参数合集
    # transactionId对于这个参数，除了要放baseling的值，还需要把values里的值全部放进去。
    for name, value in endpoint["baseline"].items(): #遍历baseline有多少个参数
        if name != param["name"]: # param是一个参数，如果当前baseline不一致的话，就把这个baseline放进去
            others[name] = value

    # 到这里，others里放的都是除了需要被替换参数，其他参数的baseline值
    # 下面是要准备被替换坏值的参数合集，baseline也要算一个,并且需要标注传入的值是否是baseline的
    # candidate里的就是(需传入的value，是否为baseline值)
    baseline_value = endpoint["baseline"][param["name"]]
    candidates = [(baseline_value, True)]
    candidates += [(value, False) for value in VALUES[param["kind"]]]

    # 把others和candidate拼接起来
    for value, is_baseline in candidates:
        yield {
            "endpoint": endpoint["name"],
            "param": param["name"],
            "value": value,
            "is_baseline": is_baseline,
            "url": render(endpoint["path"], {**others, param["name"]: value}),
        }
    return



"""
主要是把config里写好的接口参数，加上baseline与values中的参数进行组合
形成多个测试用例，所以对于config中的参数。
1.第一条是要正常的，即全部拿baseline中的值
2.后面开始控制变量，每次对一个参数放入坏值
"""
def gen_case():
    for endpoint in ENDPOINTS: #遍历所有接口
        for param in endpoint["params"]: #遍历所有接口的每一个参数，这些参数是需要被替换放坏值进去的
            yield from param_cases(endpoint, param)

