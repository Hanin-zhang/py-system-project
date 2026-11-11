"""配电网光伏承载力平台 · 计算服务。

模块分层（只允许上层调下层，禁止跨层）：

    case_loader  ->  powerflow  ->  timeseries  ->  hc_solver  ->  mitigation
"""
