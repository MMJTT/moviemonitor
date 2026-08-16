import requests
from bs4 import BeautifulSoup
import smtplib
import time
import os
from email.mime.text import MIMEText
from email.header import Header

# --- 1. 配置区：请修改为你自己的信息 ---

# 你要监控的网址
TARGET_URL = "https://www.maoyan.com/cinemas?brandId=357343&movieId=1142033"

# 你要查找的关键词
KEYWORD = "前滩太古里店" # 你之前的截图显示是“宝龙店”，请确保这里填你真正要查的

# 刷新间隔（秒） - 【警告】5秒非常危险，容易被封IP！
REFRESH_INTERVAL = 60 # 你要求的值 (如果你被封了，请改成 300 或 600)

# --- 邮件发送配置 (163邮箱) ---
EMAIL_HOST = "smtp.163.com"          # 163 SMTP 服务器
EMAIL_PORT = 465                      # SSL 端口
EMAIL_USER = os.environ.get('EMAIL_USER')  # 你的163邮箱地址
EMAIL_PASS = os.environ.get('EMAIL_PASS')  # 你的163 SMTP 授权码

# --- 邮件接收配置 ---
RECEIVER_EMAIL = "850634546@qq.com" # 你的收件箱地址
EMAIL_SUBJECT = "关键词出现了！"

# --- 2. 状态变量 ---
# 用于防止重复发送邮件
notification_sent = False

# --- 3. 核心功能：检查网页 (这是你缺失的部分) ---
def check_website():
    """
    访问网页并检查关键词。
    """
    # 声明我们要修改的是全局变量
    global notification_sent 
    
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始检查: {TARGET_URL}")
    
    try:
        # 模拟浏览器发送请求
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(TARGET_URL, headers=headers, timeout=10)
        response.raise_for_status() 
        response.encoding = 'utf-8' 

        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        # 检查关键词
        if KEYWORD in page_text:
            print(f"✅ 成功！发现关键词: '{KEYWORD}'")
            
            # 核心逻辑：只在“未发送过”的状态下才发送
            if not notification_sent:
                print(">>> 首次发现，准备发送邮件...")
                send_notification()
                notification_sent = True # 标记为已发送，防止重复
            else:
                print(">>> 关键词持续存在，但邮件已发送过，不再重复。")
        else:
            print(f"❌ 未发现关键词: '{KEYWORD}'")
            
            # 核心逻辑：如果关键词消失了，重置状态
            if notification_sent:
                print(">>> 关键词已消失，重置发送状态。")
                notification_sent = False 

    except requests.exceptions.RequestException as e:
        print(f"Error: 网站请求失败 - {e}")
    except Exception as e:
        print(f"Error: 脚本执行出错 - {e}")

# --- 4. 核心功能：发送邮件 (已修复拒收问题) ---
def send_notification():
    """
    发送邮件通知。(已修复版本)
    """
    if not EMAIL_USER or not EMAIL_PASS:
        print("错误：未设置 EMAIL_USER 或 EMAIL_PASS 环境变量。")
        return

    print("开始连接 163 邮箱 SMTP 服务器...")
    
    message_body = f"已出现关键词: 【{KEYWORD}】\n\n请尽快查看！"
    msg = MIMEText(message_body, 'plain', 'utf-8')

    # --- 【关键修复】 ---
    # 移除 'From' 和 'To' 的中文昵称，直接使用邮箱地址
    msg['From'] = EMAIL_USER            # 直接使用发件人邮箱
    msg['To'] = RECEIVER_EMAIL          # 直接使用收件人邮箱
    
    # 'Subject'（主题）包含中文，必须保留 Header 编码
    msg['Subject'] = Header(EMAIL_SUBJECT, 'utf-8')
    # --- 【修复结束】 ---

    try:
        # 连接到 SMTP 服务器 (使用 SSL)
        server = smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [RECEIVER_EMAIL], msg.as_string())
        server.quit()
        print("🎉 邮件发送成功！")
        
    except smtplib.SMTPException as e:
        print(f"Error: 邮件发送失败 - {e}")
    except Exception as e:
        print(f"Error: 邮件配置错误 - {e}")

# --- 5. 运行主循环 ---
if __name__ == "__main__":
    # 启动前检查环境变量
    if not EMAIL_USER or not EMAIL_PASS:
        print("错误：启动失败，请先设置 EMAIL_USER 和 EMAIL_PASS 环境变量！")
    else:
        print(f"--- 网页监控脚本启动 ---")
        print(f"监控目标: {TARGET_URL}")
        print(f"监控关键词: {KEYWORD}")
        print(f"刷新间隔: {REFRESH_INTERVAL} 秒")
        print(f"发件邮箱: {EMAIL_USER} (163)")
        print("---------------------------")
        
        # 开始无限循环
        while True:
            check_website()
            print(f"--- 等待 {REFRESH_INTERVAL} 秒后进行下一次检查 ---")
            time.sleep(REFRESH_INTERVAL)