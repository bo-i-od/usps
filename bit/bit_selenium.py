from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from bit_api import *
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

# /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
res = openBrowser("050c888d54cf408da6d1a5307ddfa233") # 窗口ID从窗口配置界面中复制，或者api创建后返回

print(res)

driverPath = res['data']['driver']
debuggerAddress = res['data']['http']

# selenium 连接代码
chrome_options = webdriver.ChromeOptions()
chrome_options.add_experimental_option("debuggerAddress", debuggerAddress)

chrome_service = Service(driverPath)
driver = webdriver.Chrome(service=chrome_service, options=chrome_options)

# 以下为PC模式下，打开baidu，输入 BitBrowser，点击搜索的案例
driver.get('https://www.baidu.com/')

input = driver.find_element(By.CLASS_NAME, 's_ipt')
input.send_keys('BitBrowser')

print('before click...')

btn = driver.find_element(By.CLASS_NAME, 's_btn')
btn.click()

print('after click')