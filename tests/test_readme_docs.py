from pathlib import Path

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_readme_names_private_server_as_the_supported_product_path():
    assert "当前支持的产品运行方式是邀请制私有服务器部署" in README
    assert "SSH 隧道" in README
    assert "PostgreSQL、Redis 和独立 Worker" in README
    assert "一次性邀请链接" in README
    assert "只能查看和操作自己的任务" in README


def test_readme_does_not_present_runlocal_as_agent_mail_product_runtime():
    assert "`runlocal` 仅用于开发页面和本地调试" in README
    assert "不能验证 Agent Mail" in README
    assert "不能创建生产监控任务" in README
    assert "Worker 是唯一持有 Agent Mail CLI 和 keyring 的服务" in README


def test_readme_documents_shared_sender_and_per_user_recipient():
    assert "固定使用 `mijiatong@agent.qq.com` 发件" in README
    assert "发送到用户注册时绑定的受邀邮箱" in README
    assert "不需要提供个人 SMTP" in README
    assert "MAX_ACTIVE_TASKS_PER_USER" in README
