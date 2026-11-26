# 数字图像处理课程竞赛平台（DIP-Competition）

面向《数字图像处理》课程的轻量级算法竞赛与图像质量评价平台：学生本地运行算法、上传结果图像，服务器自动计算客观指标、维护排行榜，并在截止后组织**匿名两两比较**的主观评价。

当前部署：`lf2`（mathlf2.lflab.cn / 222.204.244.237），目录 `/home/jlyao/Desktop/DIP-Competition`，Gunicorn 监听 `8000` 端口，systemd 用户服务 `dip-competition` 常驻。
访问地址：<http://222.204.244.237:8000>；教师后台：<http://222.204.244.237:8000/admin/>（账号 `teacher`，密码见交付说明）。

---

## 一、功能

**学生端**
- 学号登录（初始密码由教师下发，首次登录强制改密）
- 任务页下载输入图像、上传结果图，上传即评分，最多 5 次/图
- 确认「最终作品」后参与排名，截止前可重新提交并重新确认
- 三类排行榜：去噪（平均 PSNR，SSIM 同分依据）、去色客观（平均 E-score）、去色主观（Bradley–Terry）
- 匿名互评：同一原图的两张灰度结果两两比较，A/B 位置随机，不显示作者与指标
- 我的提交：全部历史、逐图指标、主观得分与名次

**教师端**
- Django 管理后台：学生/提交/互评/排名管理，提交预览、重新评分、最终标记
- 运行设置：在线覆盖提交与互评时间、排行榜公开开关、去色综合分权重
- 教师面板 `/staff/`：互评进度、覆盖度、一键分配互评、重新拟合排名、成绩与作品导出
- 批量导入学生、生成初始密码表、重置密码、导出 CSV / 最终作品 ZIP

---

## 二、技术栈

| 层 | 选型 | 用途 |
|---|---|---|
| Web | Django 5.2 + Gunicorn | 认证、提交、排行榜、互评、后台 |
| 指标 | scikit-image / NumPy / SciPy | PSNR、SSIM、CCPR、CCFR、E-score、BT 拟合 |
| 图像 | Pillow、OpenCV(headless) | 读写、格式与通道校验、基线算法 |
| 数据库 | SQLite（WAL） | 单班规模足够；可切换到 PostgreSQL |
| 静态资源 | WhiteNoise | 无需额外 Web 服务器即可上线 |
| 部署 | systemd --user + Gunicorn | 常驻、崩溃自动重启 |

不引入 Redis / Celery / Kubernetes / GPU：评测是毫秒到数百毫秒级的纯 CPU 计算。

---

## 三、目录结构

```
DIP-Competition/
├── manage.py
├── requirements.txt          # 与实际部署环境一致的版本锁定
├── .env / .env.example       # 密钥、域名、数据库配置
├── config/competition.yaml   # 竞赛规则、任务、指标、赛程（唯一事实来源）
├── web/                      # settings / urls / wsgi
├── competition/
│   ├── models.py             # Profile / Submission / ReviewAssignment / Comparison ...
│   ├── views.py  urls.py  forms.py  admin.py
│   ├── config.py             # yaml 加载与缓存
│   ├── imaging.py            # 图像校验与读写
│   ├── metrics/denoise.py    # PSNR / SSIM
│   ├── metrics/decolor.py    # CCPR / CCFR / E-score（IJCV14 代码移植）
│   ├── peer_review/allocation.py   # 互评任务分配
│   ├── peer_review/ranking.py      # Bradley–Terry（MM 迭代）
│   ├── services/             # 评分、提交、排行榜、互评、导出、赛程
│   └── management/commands/  # prepare_data / import_students / run_baselines / ...
├── templates/                # 学生端 + 教师面板 + 作品讲评 + 错误页
├── static/css/app.css
├── data/
│   ├── public/denoise/D01.png, D02.png      # 公开输入
│   ├── public/decolor/C01.png, C02.png
│   └── private/ground_truth/D01.png, D02.png # 私有参考，任何 URL 都不暴露
├── storage/                   # 数据库、提交图像、基线、日志
├── deploy/                    # systemd / Caddy / Nginx 示例
└── tests/                     # 指标、提交、互评、排名测试
```

---

## 四、指标实现

### 4.1 去噪：PSNR / SSIM

固定 8-bit sRGB、`data_range=255`、SSIM `win_size=7`、`channel_axis=-1`，调用 `skimage.metrics`。
不允许改尺寸、不允许自动归一化：提交图必须与输入图逐像素对齐。

### 4.2 去色：CCPR / CCFR / E-score

移植自 Cewu Lu、Li Xu、Jiaya Jia（IJCV 2014）官方 MATLAB 代码 `metric.m`：

- 颜色对比：CIELab 空间随机像素对的欧氏距离 ΔE（官方每像素 10 对，随机种子固定）
- 内容对比：Lab 三通道 `[1,-1]`、`[1;-1]` 差分的合成梯度幅值
- 灰度对比：灰度图转 sRGB→Lab 的 L 通道，像素对差与梯度

```
CCPR(τ) = #{ΔE > τ 且 |Δg| > τ} / #{ΔE > τ}
CCFR(τ) = 1 - #{|∇g| > τ 且 ΔE < τ} / #{|∇g| > τ}
E-score = 2·CCPR·CCFR / (CCPR + CCFR)
```

默认 `τ = 5`（论文常用值，官方代码遍历 1..40）。所有学生共享同一组像素对，评分完全可复现。
与原 MATLAB 的差异仅在随机数发生器（NumPy 替代 MATLAB `randperm`），因此分数不追求逐位一致，但统计意义与排序结论一致。

### 4.3 主观评价：Bradley–Terry

对每张原图单独拟合潜在质量参数 θ：

```
P(i ≻ j) = exp(θ_i) / (exp(θ_i) + exp(θ_j))
```

用 MM 迭代求解、加 L2 正则抑制全胜/全负发散，平局按 0.5 胜。
每张图相对其余作品的**平均预测胜率**映射为 0–100，两张图取平均。
去色综合参考分 `Q = 100 × (0.4 × 平均E + 0.6 × 主观分/100)`，权重可在后台修改，仅作参考，不覆盖两个独立榜单。

---

## 五、部署（已在该服务器执行）

```bash
# 1. 代码与虚拟环境
cd ~/Desktop/DIP-Competition
python3 -m venv --without-pip .venv && .venv/bin/python /tmp/get-pip.py
.venv/bin/python -m pip install -r requirements.txt

# 2. 环境变量
cp .env.example .env && vim .env    # 改 SECRET_KEY / ALLOWED_HOSTS / CSRF

# 3. 数据（SIDD + CADIK24 → 公开输入与私有参考）
.venv/bin/python manage.py prepare_data \
  --sidd-root /mnt/share/public/Datasets/SIDD \
  --cadik-root data/_sources/cadik/cadik08perceptualEvaluation-images

# 4. 数据库与账号
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py collectstatic --noinput

# 5. 基线
.venv/bin/python manage.py run_baselines

# 6. 常驻服务
mkdir -p ~/.config/systemd/user
cp deploy/dip-competition.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dip-competition
```

访问 `http://<服务器IP>:8000`。

**如需域名 + HTTPS**：把 `deploy/Caddyfile` 或 `deploy/nginx.conf` 交给管理员配置反向代理到 `127.0.0.1:8000`，把域名加入 `.env` 的 `DJANGO_ALLOWED_HOSTS` 与 `DJANGO_CSRF_TRUSTED_ORIGINS`，再设置 `DJANGO_HTTPS=1`（自动开启安全 Cookie、HSTS 与 HTTPS 跳转）。

**Docker 方式（可选）**：`docker compose up -d --build`，见 `docker-compose.yml`（默认使用 PostgreSQL，适合机房统一部署）。

---

## 六、教师操作流程

1. **导入学生**：准备 `students.csv`（`student_id,name`），执行
   `manage.py import_students --csv students.csv --out accounts.csv`，
   把 `accounts.csv` 分发给学生（含参赛编号与初始密码，权限 600）。
2. **检查数据**：`manage.py prepare_data` 已生成四张图；`/staff/` 的「上线验收」应全绿。
3. **生成基线**：`manage.py run_baselines`，排行榜底部会显示 NLM、Median、OpenCV decolor 等参照。
4. **开放比赛**：在 `/admin/competition/appsetting/1/change/` 设定时间（留空则用 yaml）。
5. **截止与冻结**：到达 `submission_close` 后提交自动关闭，最终作品不可再改。
6. **互评**：进入 `/staff/` 点击「生成互评任务」，学生在 `/review/` 完成 10 次比较。
   可用 `manage.py allocate_reviews --per-student 10 --seed 2026` 复现同一分配。
7. **出分**：`manage.py compute_rankings`（或教师面板按钮）拟合 BT，发布三个榜单。
8. **讲评**：`/staff/gallery/` 按指标列出最高与最低的作品，便于课堂讨论“指标高 ≠ 看起来自然”。
9. **导出**：`manage.py export_scores --out scores.csv --with-images finals.zip`，
   或教师面板的两个导出按钮。

### 上线前演练

```bash
python manage.py seed_demo --students 12        # 生成 12 个演示账号（密码 demo12345）
python manage.py allocate_reviews --per-student 10
python manage.py compute_rankings
python manage.py seed_demo --clear              # 演练结束，删除全部演示数据
```

演示期间如需看到互评界面，可在「运行设置」把互评时间临时改成当前时间；演练后把四个时间字段清空即恢复 yaml 赛程。

---

## 七、常用运维命令

```bash
# 服务
systemctl --user status dip-competition
systemctl --user restart dip-competition
journalctl --user -u dip-competition -f

# 日志（应用级）
tail -f storage/logs/app.log

# 备份（数据库 + 提交图像 + 私有参考）
tar czf backup-$(date +%F).tgz storage/db.sqlite3 storage/submissions data/private

# 重新评分某个提交：后台「提交」列表 → 选中 → 重新计算指标
# 重置学生密码
.venv/bin/python manage.py reset_password 20260001
```

---

## 八、安全与隐私设计

- 参考图放在 `data/private/`，没有任何 URL 映射；提交图通过带权限判断的视图返回，学生只能看自己的。
- 互评图片通过 `/files/review/<assignment_id>/<side>.png` 返回，只对分配到的评价者开放。
- 上传校验：PNG 魔数、真实格式、色彩模式（RGB / L）、通道数、尺寸逐项比对，20 MB 上限，损坏文件拒绝。
- 排行榜只显示参赛编号；真实姓名仅教师后台可见。
- 分配算法保证：不评价自己的作品、同一评价者不重复评价同一对、覆盖均衡。
- CSRF、登录态 12 小时、密码强度校验、首次登录强制改密。

---

## 九、测试

```bash
.venv/bin/python manage.py test tests -v 2
```

覆盖：PSNR 与解析解一致、SSIM 对模糊敏感、常量灰度得 0 分、指标可复现；
尺寸/通道/格式校验、次数上限、最终作品唯一性、越权访问被拒；
互评分配硬约束、Bradley–Terry 传递性、完整互评到排名的链路。

---

## 十、边界说明（需要在课堂上讲清楚）

1. 平台评价的是**结果图像质量**，无法验证算法原创性、运行效率与可复现性；学生仍可能复制他人或公开结果。
2. 去色客观指标是公开 MATLAB 代码的 Python 移植，用于**同一平台内横向比较**，不宜与论文表格中的绝对数值逐位对比。
3. CADIK24 图像来自原作者公开评测集，仅用于课程教学；SIDD 参考图仅存服务器私有目录，但公开数据集本身可被检索到，正式计分建议至少替换为一张教师自备、未公开的参考图。
4. 单班（≤ 60 人）规模下 SQLite + Gunicorn 3 worker 足够；更大规模建议切换 PostgreSQL 并增加 worker。

---

## 十一、扩展新任务（超分 / 去模糊 / 融合）

1. 在 `config/competition.yaml` 增加 `tasks` 条目（图像、通道、指标、排名指标）。
2. 把输入图放 `data/public/<slug>/`，参考图放 `data/private/ground_truth/`。
3. 在 `competition/metrics/` 增加指标函数，并在 `services/scoring.py` 的 `evaluate_path` 中按 `channels` 或 `slug` 分派。
4. 页面、排行榜、互评、导出无需改动。

---

## 十二、本次部署的实际数据与参数

| 项 | 实际值 |
|---|---|
| 去噪图 1（D01） | SIDD 场景 `0036_002_GP_06400_03200_3200_N`，768×768 裁切，真实手机噪声，初始 PSNR ≈ 20.15 dB |
| 去噪图 2（D02） | SIDD 场景 `0146_007_N6_00400_00400_4400_N` 干净参考，768×768 裁切 + 高斯噪声 σ=25/255，初始 PSNR ≈ 20.19 dB |
| 去色图 1（C01） | CADIK24 `C8TZ7768`，389×390，抽象色块图形 |
| 去色图 2（C02） | CADIK24 `fruits`，390×390，色彩丰富的自然图像 |
| 基线（去噪） | 原始含噪图 20.15 / Gaussian 25.35 / Median 26.17 / Bilateral 24.27 / NLM 24.29 / BM3D 26.14（D01，dB） |
| 基线（去色） | RGB2Gray 0.864 / OpenCV decolor 0.945 / 加权线性 0.908（C01，E-score） |
| 参考图位置 | `data/private/ground_truth/`，无 URL 暴露 |
| 数据库 | `storage/db.sqlite3`（WAL 模式） |

BM3D 基线的 σ 由 `run_baselines.py` 中的 `BM3D_SIGMA` 按图像设定（D01 真实噪声 0.15，D02 合成噪声 0.10）：
真实噪声在暗部更强，单一全局估计会低估，这一点本身可以在课堂上讲。

数据来源与裁切参数记录在 `data/private/manifest.json`，重跑 `prepare_data --force` 可复现同一批图像
（SIDD 场景通过 `--prefer` 固定，合成噪声种子固定为 20261126）。
