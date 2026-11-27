# 学术层（competition/research）

教师组要改的东西全在这里。Web 层（页面、提交、互评、导出）只调用
`evaluators.evaluate()` 和 `config.TaskSpec.public_image()/ground_truth()`，
不碰这里面的数学。

## 文件

| 文件 | 管什么 | 什么时候改 |
|---|---|---|
| `params.py` | **唯一的参数默认值**：PSNR 动态范围、SSIM 窗口、去色 τ / 采样轮数 / 种子、裁切尺寸、合成噪声 σ 与种子 | 改评分口径或题目强度 |
| `degradation.py` | 退化算子：`gaussian_noise()`（Y = clip(X + N(0,σ²), 0, 255)）、`texture_crop_box()`（选纹理最丰富的一块）、`crop_square()` | 想换加噪方式、换裁切策略 |
| `dataset.py` | 每张图的输入图 / 参考图 / 退化声明解析 | 一般不用改，改 yaml 即可 |
| `evaluators.py` | **任务 → 指标族**的显式映射（denoise / decolor） | 加新指标、换指标 |
| `sources.py` | SIDD / CADIK 原始数据的定位与配对 | 换数据集时 |

指标本身的数学实现在 `competition/metrics/denoise.py` 与 `metrics/decolor.py`，
公式写在各自文件开头，与 MATLAB 原实现一一对应。

## 常见操作

**1. 改去色指标的阈值 τ**

```python
# competition/research/params.py
DECOLOR_TAU = 5.0     # 改成 10.0 更宽松，改成 2.0 更严格
```

临时对比的话，改 `config/competition.yaml` 的 `metrics_options.decolor_tau` 即可，不用重启。

**2. 改合成噪声强度**

```yaml
# config/competition.yaml，D02 那一行
degradation: {kind: gaussian, sigma: 15, seed: 20261126, source: sidd}
```

改完必须重跑数据准备，否则磁盘上还是旧的 σ=25 图：

```bash
python manage.py prepare_data --force
```

**3. 换一张参考图 / 换文件名**

```yaml
images:
  - key: D01
    label: 图 1 · 真实噪声
    input: D01_new.png            # data/public/denoise/D01_new.png
    reference: D01_gt.png         # data/private/ground_truth/D01_gt.png
    degradation: {kind: real, source: sidd}
```

三个字段都可省略，省略时按 `<key>.png` 推导。

**4. 加一套新指标（例如 LPIPS）**

1. `competition/metrics/lpips.py` 写 `evaluate_files(gt, result, **opts) -> dict`；
2. `evaluators.py` 的 `evaluate()` 加一个 `if kind == "lpips":` 分支；
3. yaml 里把任务改成 `evaluator: lpips`，同时改 `metrics:` 列表与 `ranking_metric`。

第 3 步两个地方要一起改：页面显示什么指标、榜单按什么排序，必须对得上。

**5. 换整个任务的题目图**

把新图放到 `data/public/<任务>/`、参考图放 `data/private/ground_truth/`，
在 yaml 的 `images:` 里改 `key` / `label` / `note`，
跑 `python manage.py prepare_data` 与 `python manage.py run_baselines` 重建基线。

## 已确认约定

- 参数默认值只写在 `params.py`，`yaml` 的 `metrics_options` 是覆盖项，不是第二份默认。
- 退化一律**离线生成落盘**，评分只读文件，不在请求里现算。
- 任务与指标的对应关系显式声明为 `evaluator`；留空时按通道数推断（3→denoise，1→decolor），仅为兼容旧配置。
- 换数据集必须同时重跑 `prepare_data` 和 `run_baselines`，否则基线还是旧图上的数。
