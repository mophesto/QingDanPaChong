import requests
from bs4 import BeautifulSoup
import concurrent.futures
import threading
import json
import git

# 填入Cookie
cookie = 'bbs_sid=jm215ap6j54pkp693qs7b5vkn1; bbs_token=gFdgyEmf5QQXwLdTUkZ8jEwSS_2FmSMA4hAnwZIA1Y7LGcqF_2FpFrKtWTaa61qRtEQBnr9FiLcn6ZpjWJnaKXnYzPgK_2BFQ_3D'

# 复制Cookie里的sid
sid = 'jm215ap6j54pkp693qs7b5vkn1'
# 开始页
start = 1
# 结束页
end = 100

# 使用连接池的Session
session = requests.Session()

strings = []
lock = threading.Lock()

def get_remote_head():
    head_dict = {}
    for i in git.Repo().git.ls_remote('--head', 'origin').split('\n'):
        commit, head = i.split()
        head = head.split('/')[2]
        head_dict[head] = commit
    return head_dict
def check_app_repo_remote(repo):
    return str(repo) in get_remote_head()
def check_app_repo_local(repo):
    for branch in repo.heads:
        if branch.name == str(repo):
            return True
    return False
    
def pull_data_branch():
    if not check_app_repo_local('data'):
        if check_app_repo_remote('data'):
            print('Pulling remote data branch!')
            git.Repo().git.fetch('origin', 'data:origin_data')
            git.Repo().git.worktree('add', '-b', 'data', 'data', 'origin_data')
    
def send_get_request(url):
    headers = {
    'Host': '70games.net',
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
    'Cookie': cookie
    }
    response = session.get(f'https://70games.net/{url}', headers=headers)
    response.raise_for_status()

    return response.text

def process_page(page_number):
    global zhanghaoliebiao
    try:
        href_values = []
        data = send_get_request(f'forum-1-{page_number}.htm?digest=2')
        soup = BeautifulSoup(data, 'html.parser')
        elements = soup.select('a.post_title')

        for element in elements:
            href_values.append(element['href'])

        for href_value in href_values:
            first_number = int(''.join(filter(str.isdigit, href_value)))
            post_data = {
                'doctype': '1',
                'return_html': '1',
                'quotepid': '0',
                'sid': sid,
                'message': '2%0D%0A'
            }
            session.post(f'https://70games.net/post-create-{first_number}-1.htm', data=post_data)

            values = []
            data = send_get_request(href_value)
            soup = BeautifulSoup(data, 'html.parser')
            elements = soup.select('.coded.col')

            for element in elements:
                values.append(element['value'])

            if len(values) == 2:
                account_info = f'账号 {values[0]} 密码 {values[1]}'
                with lock:
                    zhanghaoliebiao.append([values[0], values[1]])
                    print(account_info)
    except Exception as error:
        print(error)

def get_data():
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(process_page, range(start, end))

zhanghaoliebiao = []
get_data()

zhangmi_path = "data/users.json"

# 读取json
with open(zhangmi_path, 'r', encoding='utf-8') as file:
    zhangmi_data = json.load(file)

# 更新数据
for zhanghao in zhanghaoliebiao:
    zhangmi_data[zhanghao[0]] = [zhanghao[1], "null"]

# 写回json
with open(zhangmi_path, 'w', encoding='utf-8') as file:
    json.dump(zhangmi_data, file, ensure_ascii=False, indent=2)

