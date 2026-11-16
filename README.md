# HIMA Info

鸿蒙智行车辆信息工具（只读）。单文件 Python 命令行工具，通过华为 OMP 会话和各品牌车辆云 IVCS 接口读取车辆列表、实时状态和账号下的只读数据。

支持按 `enterpriseCode` 路由到不同品牌：

| enterpriseCode | 品牌 | 车辆云地址 | 品牌 CA |
|---|---|---|---|
| SERES | 问界 | `apig.fgaiservice.com` | `brand-seres.pem` |
| CHERY | 智界 | `apir.chssatsp.icvcs.com` | `brand-chery.pem` |
| BJEV | 享界 | `apir.bjevssa.icvcs.com` | `brand-bjev.pem` |
| JAC | 尊界 | `apir.jacssa.icvcs.com` | `brand-jac.pem` |
| SRIH | 上汽 | `api-app.srih.icvcs.com` | `brand-saic.pem` |

> 非官方工具，仅用于读取本人账号下的车辆数据。接口可能随时变更；不要用于车辆控制或高频请求。

## 环境与安装

要求：

- Python 3.9+
- `requests`

安装：

```bash
cd hima-info
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

安装后所有命令都在虚拟环境中执行：

```bash
python hima_info.py --help
```

## 目录结构

```text
hima-info/
├── hima_info.py           # 主程序（单文件，无 token/密钥）
├── requirements.txt       # Python 依赖
├── README.md              # 本文档
├── .gitignore             # 忽略 session.json、缓存、虚拟环境
├── session.json           # 运行时凭据（敏感，0600，勿提交）
├── session.lock           # 空锁文件，单飞控制用
└── certs/
    ├── brand-seres.pem    # 问界 SOKON DFSK Sub CA
    ├── brand-chery.pem    # 智界 CHERY SSA Sub CA
    ├── brand-bjev.pem     # 享界 BJEV SSA Sub CA
    ├── brand-jac.pem      # 尊界 JAC SSA Sub CA
    └── brand-saic.pem     # 上汽 SRIH Sub CA
```

## 快速开始

第一次使用：

```bash
# 1. 只打印授权链接（推荐先复制到浏览器）
python hima_info.py login --url

# 2. 浏览器登录华为账号并同意授权后，把跳转到的整条回调 URL 粘贴回来
python hima_info.py login --code 'https://xop.xcar.test.vmall.com/?code=...'
```

也可以直接运行 `python hima_info.py login`，程序会打印授权链接并等待粘贴。

登录后：

```bash
python hima_info.py vehicles          # 车辆列表，带序号
python hima_info.py info 1           # 第 1 辆车的实时摘要
python hima_info.py info 2           # 第 2 辆车
python hima_info.py info 1 --full    # 第 1 辆车的全部只读数据（JSON）
```

日常保活：

```bash
python hima_info.py refresh --session-only
```

## 命令

| 长命令 | 短命令 | 说明 |
|---|---|---|
| `login` | `l` | 生成授权链接并保存会话 |
| `refresh` | `r` | 刷新用户会话和车辆 token |
| `vehicles` | `v` | 车辆列表（带序号） |
| `info` | `i` | 实时车辆信息（默认第一辆车） |

### `login`

```bash
python hima_info.py login             # 打印授权链接并等待粘贴回调 URL/code
python hima_info.py login --url       # 只打印授权链接，不登录
python hima_info.py login --code URL  # 非交互：直接传回调 URL 或 code
```

- `--url` 只输出授权链接，适合在另一台设备/浏览器中打开。
- `--code` 接受完整回调 URL，也接受裸 `code`。
- 登录成功后会立即签发各品牌车辆 token，并尝试列出车辆。

### `refresh`

```bash
python hima_info.py refresh --session-only  # 只滚动用户会话（推荐定时保活）
python hima_info.py refresh                 # 刷新用户会话 + 各品牌车辆 token
python hima_info.py refresh --force         # 强制重新签发车辆 token（可能踢手机）
```

- `--session-only`：只调用 `user/refresh`，不调用 `vehicle/auth`，不踢手机，适合 6~12 小时一次的串行保活。
- 默认 `refresh`：先滚用户会话，再对每个品牌调用 `vehicle/refresh`。车辆 token 仍有效时服务端可能原样返回旧 token 和旧到期时间，这是正常的，不代表没刷新。
- `--force`：走 `vehicle/auth` 重新签发车辆 token，到期时间会变，但可能注销手机上的车辆 token（100015）。
- `--session-only` 和 `--force` 互斥。

### `vehicles`

```bash
python hima_info.py vehicles
```

输出：

```text
[1] 问界·SERES  粤A *****  VIN L********  vehicleId ****
[2] 智界·CHERY  粤B *****  VIN L********  vehicleId ****
```

序号可直接用于 `info`。

`vehicles` 和 `info` 都会自动检查车辆 token：token 缺失、已过期或剩余不足 1 小时时，自动刷新用户会话，并续期或重签车辆 token，**不需要手动执行 `refresh`**。如果请求过程中服务端仍判定车辆 token 失效（401），会自动再刷新并重试一次。

自动刷新会签发新 token，可能把同时在线的手机顶下线；如果返回 `10022`（refreshToken 本身失效），自动刷新也无法恢复，只能重新 `login`。

### `info`

```bash
python hima_info.py info                    # 第 1 辆车
python hima_info.py info 2                  # 第 2 辆车
python hima_info.py info --vehicle-id ID    # 按 vehicleId 指定
python hima_info.py info 2 --full           # 全部只读数据（JSON）
```

`info` 默认输出人可读摘要；`--full` 输出全部只读区块的 JSON。`--full` 是唯一的 JSON 输出模式，没有单独的 `--json` 参数。

### `bundle`

导出 TeslaMate 登录页需要的 HIMA 凭据包：

```bash
python hima_info.py bundle
# 默认直接打印第一辆车的 base64 凭据包

python hima_info.py bundle | pbcopy        # 直接复制到剪贴板
python hima_info.py bundle 2               # 第二辆车
python hima_info.py bundle --vehicle-id ID
python hima_info.py bundle --format json   # 明文 JSON
python hima_info.py bundle --output hima_bundle.txt  # 可选：写 0600 文件
```

默认输出 base64 单行，直接粘贴到 TeslaMate 登录页的 **Access Token**；**Refresh Token** 填 `HIMA`。

输出含车辆 token 和用户 token，**不要提交、录屏或分享**；`--output` 生成的文件已在 `.gitignore` 中。

## 登录逻辑

### 1. 生成授权 URL

`login` / `login --url` 会生成：

```text
https://oauth-login.cloud.huawei.com/oauth2/v3/authorize
  ?access_type=offline
  &response_type=code
  &client_id=****
  &ui_locales=zh-cn
  &redirect_uri=https%3A%2F%2Fxop.xcar.test.vmall.com%2F
  &state=<随机 16 位 hex>
  &scope=<完整 scope>
  &display=touch
  &nonce=<随机 16 位 hex>
  ...
```

`state` 和 `nonce` 每次随机；`client_id`、`redirect_uri`、`scope` 必须与 App 注册值一致。

### 2. 浏览器登录

在浏览器打开授权 URL，用华为账号登录并同意授权。成功后浏览器会跳到：

```text
https://xop.xcar.test.vmall.com/?code=...
```

这个域名是**固定回调占位**：

- 它不会真的打开一个可用页面，浏览器提示打不开是正常现象；
- 不要把 `redirect_uri` 改成别的域名或路径，否则华为会返回 `invalid redirect_uri`；
- 只需要从地址栏复制整条 URL，程序会自己提取 `code` 参数。

### 3. 用 code 换 OMP 会话

`login --code` 或粘贴回调 URL 后：

```text
code
  → POST /xcar/omp/xbs/account/user/auth
  → sessionKey（约 20 分钟）
  → oauthToken.accessToken / refreshToken（用户 at/rt）
  → userId、userInfo.sessionKeyStatus
```

请求会带 Android 客户端身份和设备信息：

- `clientType = "android"`
- `device.id = SHA-256(稳定设备串).upper()`，默认取 macOS 硬件 UUID，可用环境变量 `AITO_DEVICE_STR` 覆盖；
- `device.model` 默认 `Pixel 7`，可用 `AITO_DEVICE_MODEL` 覆盖；
- `kickOut = false`，是否踢设备由后面的 `sessionKeyStatus` 决定。

### 4. `sessionKeyStatus = 1`

`sessionKeyStatus = 1` 表示当前设备不是受信设备，不是会话失效：

- `user/refresh` 仍然会成功，并返回新的 `sessionKey`；
- 但紧接着 `vehicle/auth` 会报 OMP `10011`（未登录/登录过期）；
- 程序会调用 `user/kickout` 把同账号在线设备顶掉，把状态转为 `0`，然后继续签发车辆 token；
- 副作用：手机 App 会被踢下线，手机重新登录后也可能把这边顶掉。这是账号“单活会话”的固有限制。

### 5. 签发各品牌车辆 token

程序对同一 OMP 会话执行两次 `vehicle/auth`：

- `EC = ""`：问界/SERES 身份；
- `EC = "ALL"`：鸿蒙智行多品牌身份。

后一次 `vehicle/auth` 会重绑定会话，可能让先签发的品牌 token 失效，因此对较早的分组再用 `vehicle/refresh` 原地补签。最终按 `accountId` 去重，写入 `session.json`。

### 6. 保存会话

`session.json`（0600）保存：

```json
{
  "userAccessToken": "...",
  "userRefreshToken": "...",
  "userId": "...",
  "sessionKey": "...",
  "sessionKeyExpiresAtUnix": 0,
  "deviceId": "...",
  "deviceModel": "Pixel 7",
  "accounts": [
    {
      "enterpriseCode": "SERES",
      "accountId": "...",
      "vehicleAccessToken": "...",
      "vehicleTokenExpiresAt": 0
    }
  ]
}
```

为什么不能只保存 `userRefreshToken`：

- OMP 的 `user/refresh` 入参是 `at + rt`；
- 请求头必须是 `xid = 当前 sessionKey`、`uid = userId`，并带设备身份；
- 车辆 token 只能由 OMP 会话签发，不能直接用华为 RT 换；
- 车辆 token 只是缓存，丢了可以重签，但缓存它可以避免频繁签发把手机顶下线。

## 刷新与单活会话

### `refresh --session-only`

```text
user/refresh(at, rt, xid=sessionKey, uid=userId, device, EC)
  → 新 at / rt
  → 新 sessionKey（约 20 分钟）
```

不调用 `vehicle/auth`，不踢设备，手机可以继续在线。建议每 6~12 小时串行执行一次。

### 普通 `refresh`

```text
user/refresh
  → 对每个品牌 vehicle/refresh
  → 车辆 token 还有效时：可能原样返回旧 token / 旧到期时间
  → 车辆 token 失效时：签发新 token
```

### `refresh --force`

```text
user/refresh
  → vehicle/auth（EC="" 和 EC="ALL"）
  → 重新签发所有品牌车辆 token
```

到期时间会变，但会注销手机上的旧车辆 token，可能把手机顶下线。

### 错误码

| 现象 | 含义 | 处理 |
|---|---|---|
| `10022` | refreshToken 失效/过期 | 重新 `login` |
| `10011` 或 `sessionKeyStatus=1` | 当前设备未受信 | 程序自动 `user/kickout` 后重试 |
| `100015` | 车辆 token 被其他客户端注销 | 等待对方安静；不要并发签发 |
| 401 / token invalid | 车辆 token 失效 | 程序自动 `refresh` 后重试一次 |
| `invalid redirect_uri` | 改了固定回调地址 | 改回 `https://xop.xcar.test.vmall.com/` |

所有会签发 token 的操作都走 `session.lock` 文件锁，同一时间只允许一个进程刷新，避免并发签发互相注销。

## 数据内容

### `info` 摘要

```text
车辆 <车牌>  VIN <VIN>  vehicleId ****
里程 <km>  速度 <km/h>  档位 <文字> (<数字>)
电池 SOC <百分比>%  续航 <km>
充电 状态 <状态> (<数字>)  电流 <A>  电压 <V>  功率 <kW>  剩余 <分钟/小时>
位置 <经度>,<纬度>  海拔 <m>  方向 <度>  卫星 <数量>
胎压 左前 <bar> 右前 <bar> 左后 <bar> 右后 <bar>
门窗 中控锁 <状态>  后备箱 <状态>  充电口盖 <状态>  前舱盖 <状态>
空调 状态 <状态>  车内 <°C>  车外 <°C>  PM2.5 <车内>/<车外>
油量 <百分比>%  油箱 <L>        # 仅油车/混动有油箱数据时显示
```

字段映射：

| 字段 | 值 |
|---|---|
| `vehicleStatus.gearSignal` | `0=N`、`13=R`、`14=D`、`15=P`，其他显示原值 |
| `vehicleStatus.sumRemainingMileage` | 表显剩余续航；缺失时用 `charge.vcuSumRemainingMileage` |
| `charge.acChargeStatus` / `dcChargeStatus` | 取较大值：`0=未充电`、`1=准备充电`、`5=充电异常`、`6=充电中`、`7=充电结束`、`18=充电中（绝缘检测）`、`25=预约充电`；已插枪但状态为 0 时显示“已连接未充电” |
| `charge.dcChargeCurrent` / `acChargeCurrent` / `chargeCurrent` | 充电电流，优先取正在充电的一侧，单位 A |
| `charge.chargeVoltage` | 充电电压，单位 V |
| `charge.chargePower` | 充电功率，单位 kW；该字段为 0 时按 `chargeVoltage × 电流 / 1000` 计算 |
| `charge.remainChargeTime` | 剩余充电时间，单位分钟；小于 60 显示分钟，超过 60 显示小时/分钟 |
| `door.centerLock` | `0=锁闭`、`1=开锁` |
| `door.trunk` / `chargeHood` / `frondHood` | `0=关闭`、`1=打开` |
| `hvac.acStatus` | `0=关闭`、`1=打开` |
| 温度 | 原始值除以 10，单位 °C |
| 胎压 | 原始压力，单位 bar |

部分车型直流充电时，服务端返回的 `charge.chargeStatus` 仍为 `0`、`chargePower` 仍为 `0.0`，因此摘要不使用这两个字段，而是以 `dcChargeStatus`/`acChargeStatus` 判断状态，并用电压 × 电流计算实际功率。

车辆离线或休眠时，很多字段是默认值（例如 SOC 可能是 `-1`），摘要会按原值显示。

### `info --full` 区块

`--full` 输出原始 JSON，顶层结构：

| 区块 | 接口 | 内容 |
|---|---|---|
| `vehicle` | 本地选择结果 | 当前车辆的车牌、VIN、vehicleId、品牌 |
| `vehicles` | `GET /vcam/v1/accounts/vehicles` | 账号下车辆列表和授权信息 |
| `account.base` | `GET /vcam/v1/accounts/base` | 账号资料（昵称、手机、邮箱、性别、生日等） |
| `account.sales` | `POST /vcam/v1/accounts/sales/query` | 车辆销售/车型档案 |
| `account.device` | `POST /vcam/v1/accounts/vehicle-infos/device/query` | 数字钥匙模块信息 |
| `status` | `POST /vctrl/v2/controls/dynamic-infos` | 完整动态数据 |
| `protectConfig` | `GET /vctrl/v1/controls/protect/config` | 隐私保护配置/支持的消息类型 |
| `maintenance.history` | `POST /vcam/v1/vehicle-use/repair/maintain/history` | 历史保养记录 |
| `maintenance.latest` | `POST /vcam/v1/vehicle-use/repair/maintain/latest` | 最近保养、下次保养里程/日期 |
| `reservations` | `POST /vcam/v1/vehicle-use/repair/reservation/operation/query/list` | 维保预约 |
| `sim` | `GET /vcam/v1/mno/sim/data/usage` | 车机 SIM 流量使用情况 |
| `dashcam` | `GET /vcam/v1/dashcams/media` | 行车记录；当前车型返回 404，属正常 |
| `geofence` | `GET /vctrl/v1/geofences` | 地理围栏列表 |

未纳入的接口：

- `/vcam/v1/accounts/simple`：需要 App 内账号搜索参数 `searchInfo`，不属于车辆数据；
- 车辆控制、车锁、空调控制等写操作：本工具只读，不实现。

### 动态数据分类

`status` 区块对应 `dynamic-infos` 的 15 个类别：

- `vehicleStatus`：里程、速度、档位、SOC、续航、电源/连接状态、告警等
- `charge`：充电状态、功率、电压/电流、充电计划、剩余充电时间、充电口状态
- `location`：经纬度、海拔、方向、卫星数、GPS 时间
- `door`：中控锁、四门、后备箱、前舱盖、充电口盖、后视镜等
- `window`：车窗、天窗、遮阳帘开度
- `tire`：四轮胎压/状态
- `seat`：各座椅状态
- `lamp`：远近光、转向灯、位置灯
- `hvac`：空调、温度、PM2.5、空气质量、香氛、空气净化
- `fuel`：油量、油耗、油箱（油车/混动）
- `welcome`、`departurePlan`、`airConditionPlan`、`warmCoolingBox`、`sentryPlan`：迎宾、出发计划、空调计划、冷暖箱、哨兵计划

## 文件说明

| 文件 | 是否敏感 | 说明 |
|---|---|---|
| `hima_info.py` | 否 | 主程序。只有公开的接口地址、client_id 等公开标识，没有 token、密码或 cookie 值 |
| `session.json` | **是** | 含 user at/rt、sessionKey、userId、deviceId、各品牌车辆 token。权限必须是 0600，禁止提交或发给他人 |
| `session.lock` | 否 | 0 字节文件锁，仅用于多进程单飞；无任何数据 |
| `certs/brand-*.pem` | 否（公开证书） | 品牌 Sub CA 公钥证书，从 AITO APK 的 assets 提取，不含私钥；用于 TLS 校验 |
| `certs/*.key`、`server.*`、`aito-ca.*` | **是（私钥）** | 旧本地回调服务器的私钥/自签证书，已删除；不要再生成或提交 |
| `requirements.txt` | 否 | 仅 `requests` |
| `.gitignore` | 否 | 已忽略 `session.json`、`session.lock`、缓存和虚拟环境 |
| `README.md` | 否 | 本文档 |

### 证书来源

`certs/brand-*.pem` 来自 AITO Android APK 的 `assets/`：

| APK 文件 | 项目文件 | 品牌 |
|---|---|---|
| `pro.crt` | `brand-seres.pem` | 问界（SOKON DFSK Sub CA） |
| `commx2.crt` | `brand-chery.pem` | 智界（CHERY SSA Sub CA） |
| `commx4.crt` | `brand-bjev.pem` | 享界（BJEV SSA Sub CA） |
| `commx6.crt` | `brand-jac.pem` | 尊界（JAC SSA Sub CA） |
| `commsaic.crt` | `brand-saic.pem` | 上汽（SRIH Sub CA） |

它们是 App 自己内置的信任锚，只有公钥和签名，没有私钥。缺少某个品牌的 CA 时：

- SERES：退化为叶子证书 SHA-256 pinning，仍可工作；
- 其他品牌：直接报“缺少品牌 CA 证书”。

## 安全与 GitHub 发布清单

### 放到 GitHub 前

1. 确认 `.gitignore` 包含 `session.json`、`session.lock`、`__pycache__/`、`.venv/`。
2. 检查仓库中没有 `session.json`、cookie、token 或车辆位置数据。
3. 检查没有私钥：

   ```bash
   grep -R "PRIVATE KEY" .
   ```

4. `certs/brand-*.pem` 是公开 CA 证书，可以提交；但请确认其中没有混入 `.key` 私钥文件。
5. README、示例输出中不要出现真实车牌、VIN、vehicleId、手机号、邮箱、位置。
6. `--full` 的 `account.base` 会返回手机号/邮箱等账号资料；不要把 `--full` 输出直接提交或贴到公开 issue。

### 不要提交的内容

- `session.json`
- 任何 `*.key`、`*.p12`、`*.pfx`
- 任何旧的 cookie/token/位置数据
- `--full` 的实际输出
- 浏览器导出的 cookie、抓包文件、APK 解包目录

## 免责声明

本项目仅供学习与研究使用。使用本工具即表示你自行承担全部风险，包括但不限于：

- 华为/鸿蒙智行账号被风控、警告、限制或封禁；
- 手机 App 或其他客户端被踢下线、会话失效、车辆 token 被注销；
- 车辆被远程唤醒、产生流量或电量消耗；
- 车辆功能异常、数据错误或车辆损坏；
- 因使用或无法使用本工具造成的任何直接或间接损失。

作者不对上述任何后果承担责任。请仅在你本人拥有或已获得明确授权的车辆上使用，并自行遵守相关服务条款和法律法规。**不要用于车辆控制或高频请求。**

## 常见问题

### `ModuleNotFoundError: No module named 'requests'`

使用虚拟环境安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 浏览器打开授权链接后报错 / 页面不存在

正常。回调地址 `xop.xcar.test.vmall.com` 是固定的测试占位域名，用来把 `code` 显示在地址栏。复制整条 URL 或其中的 `code` 即可，不需要页面能打开。

### 能改回调 URL 吗？

不能。`redirect_uri` 必须与华为为该 App 注册的值完全一致；改成别的域名会得到 `invalid redirect_uri`。`xop.xcar.test.vmall.com` 本身就是 fake/占位回调，不打开展示页面是预期行为。

### `10022`

refreshToken 失效，只能重新登录：

```bash
python hima_info.py login --url
python hima_info.py login --code '<回调 URL 或 code>'
```

### 手机被踢下线 / 手机重新登录后这边失效

账号是单活会话，手机和脚本会互相顶。日常只跑：

```bash
python hima_info.py refresh --session-only
```

需要新车辆 token 时，才运行完整 `refresh` 或 `refresh --force`。

### `100015`

车辆 token 被另一个客户端注销。不要并发刷新；等对方安静后重试。容器/定时任务应串行并保留冷却时间。

### 提示“缺少品牌 CA 证书”

确认 `certs/` 下有对应 `brand-*.pem`。当前两辆车至少需要：

- 问界：`brand-seres.pem`
- 智界：`brand-chery.pem`

### `dashcam` 返回 404

当前车型/品牌没有开放行车记录接口，属预期结果，不影响其他数据。

### 车辆离线，数据都是 0 或 -1

车辆休眠/离线时接口返回默认值。等车辆上线后再读。

## 限制

- 只读工具，不提供车辆控制能力。
- 依赖非公开接口，服务端字段和错误码可能变化。
- 账号单活：手机 App、脚本、TeslaMate 等客户端不能同时无限签发车辆 token。
- 不要高频调用 `vehicle/auth`；日常保活用 `--session-only`。
- 不要尝试 AITO-API 的 `loginV3`/TGC 直登路线；该路线已被华为风控限制。
