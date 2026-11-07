"""票档解析与库存判断：对项目详情的纯计算，不触碰运行状态。"""

from __future__ import annotations

from .bili import ticket
from .config import AppConfig
from .grab_models import TicketTarget

# 售罄类文案出现即判定不可售，优先级高于库存数字。
_UNAVAILABLE_WORDS = ("售罄", "缺货", "不可售", "未开售", "暂停售票", "停售", "无票")
# 库存不足以覆盖购买数量时，再看文案里有没有明确的可售信号。
_AVAILABLE_WORDS = ("购买", "选座", "预订", "可售", "销售中", "立即")


def target_from_sku(config: AppConfig, sku: ticket.TicketSku, order: int) -> TicketTarget:
    return TicketTarget(
        screen_id=config.screen_id,
        sku_id=sku.sku_id,
        desc=sku.desc,
        price=sku.price,
        sale_flag=sku.sale_flag,
        num=sku.num,
        order=order,
    )


def monitor_sku_ids(config: AppConfig) -> list[int]:
    """回流监控的候选 sku 列表，去重后保证首发票档在内。"""

    ids = list(dict.fromkeys(config.return_monitor_sku_ids or []))
    if config.sku_id > 0 and config.sku_id not in ids:
        ids.append(config.sku_id)
    return ids or [config.sku_id]


def find_target(
    config: AppConfig,
    project: ticket.Project,
    sku_id: int,
    order: int = 0,
) -> TicketTarget:
    for screen in project.screens:
        if screen.screen_id != config.screen_id:
            continue
        for sku in screen.skus:
            if sku.sku_id == sku_id:
                return target_from_sku(config, sku, order)
    raise RuntimeError("未匹配到指定场次/票档，请重新加载演出并选择票档")


def monitor_targets_from_project(config: AppConfig, project: ticket.Project) -> list[TicketTarget]:
    """按配置顺序解析全部候选票档；任一票档不在当前场次即报错。"""

    wanted_ids = monitor_sku_ids(config)
    found: dict[int, TicketTarget] = {}
    for order, sku_id in enumerate(wanted_ids):
        try:
            found[sku_id] = find_target(config, project, sku_id, order)
        except RuntimeError:
            continue

    missing = [str(sku_id) for sku_id in wanted_ids if sku_id not in found]
    if missing:
        raise RuntimeError(f"回流候选票档不在当前场次中: {', '.join(missing)}")
    return [found[sku_id] for sku_id in wanted_ids]


def available_targets(config: AppConfig, targets: list[TicketTarget]) -> list[TicketTarget]:
    """筛出疑似可售的票档，库存多的优先，同库存按配置顺序。"""

    available = [target for target in targets if looks_available(config, target)]
    return sorted(available, key=lambda target: (-target.num, target.order))


def looks_available(config: AppConfig, target: TicketTarget) -> bool:
    text = target.sale_flag.strip()
    if any(word in text for word in _UNAVAILABLE_WORDS):
        return False
    if target.num >= config.count:
        return True
    if not text:
        return False
    return any(word in text for word in _AVAILABLE_WORDS)


def sku_looks_available(config: AppConfig, sku: ticket.TicketSku) -> bool:
    return looks_available(config, target_from_sku(config, sku, 0))


def format_stock_status(target: TicketTarget) -> str:
    sale_flag = target.sale_flag or "状态未知"
    return f"{target.desc} {sale_flag} 库存提示 {target.num}"


def format_monitor_candidates(targets: list[TicketTarget]) -> str:
    if not targets:
        return "未找到回流候选票档"
    return "；".join(format_stock_status(target) for target in targets)
