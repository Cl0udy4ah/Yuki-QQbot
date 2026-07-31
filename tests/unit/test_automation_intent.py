from qq_ai_bot.automation.intent import (
    enforce_creation_claim,
    is_scheduled_automation_request,
)


def test_future_action_requests_are_scheduled_automation_intents() -> None:
    assert is_scheduled_automation_request(
        "两分钟后查询 storeCode 1410135 当前可用的早餐套餐，把价格发给我，不要下单"
    )
    assert is_scheduled_automation_request(
        "明天早上九点四十五分，在 storeCode 1410135 查询双层原味板烧鸡腿麦满分套餐"
    )
    assert is_scheduled_automation_request("每天早上九点提醒我喝水")
    assert is_scheduled_automation_request("晚点提醒我吃饭")


def test_information_about_time_is_not_mistaken_for_a_scheduled_action() -> None:
    assert not is_scheduled_automation_request("明天早餐有什么")
    assert not is_scheduled_automation_request("明天九点天气怎么样")
    assert not is_scheduled_automation_request("我每天九点起床")
    assert not is_scheduled_automation_request("查看昨天九点的聊天记录")


def test_success_claim_requires_persisted_confirmation() -> None:
    assert (
        enforce_creation_claim(
            "设好了，明天准时查询",
            scheduled_intent=True,
            persisted=False,
        )
        == "这个定时任务还没有写入任务列表，不能算创建成功。"
    )
    assert (
        enforce_creation_claim(
            "设好了，明天准时查询",
            scheduled_intent=True,
            persisted=True,
        )
        == "设好了，明天准时查询"
    )
    assert (
        enforce_creation_claim(
            "明天具体几点？",
            scheduled_intent=True,
            persisted=False,
        )
        == "明天具体几点？"
    )
