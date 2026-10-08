# 第三方依賴與授權範圍

文件更新：2026-10-08；本次重新核對 Qwen 授權與 RIFE 官方來源，ComfyUI 節點來源紀錄沿用 2026-10-05 查核。這份文件說明本專案與外部依賴的界線，不取代各上游授權條文。

## 工作台與模型是不同授權

本專案自行撰寫的工作台程式、教學與合成測試素材依 [MIT](LICENSE) 發布，可依條款修改、散布及商用。這個授權不會替第三方模型、套件、ComfyUI 或使用者素材重新授權。

**預設去背流程使用 Qwen-Image 2.1。其官方授權目前限定非商業研究／評估用途；商業使用模型須另向權利人取得授權。工作台使用 MIT 不會取消這項限制。** 模型使用授權與輸出圖片的權利也應分別確認。[官方 LICENSE](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE)

本儲存庫與 Release 不附模型權重、ComfyUI、FFmpeg、Python 安裝程式或第三方套件二進位檔；安裝腳本只在本機虛擬環境安裝 requirements.txt 指定套件。

## 外部依賴

| 依賴 | 用途 | 官方來源 |
| --- | --- | --- |
| Python 3.12 | 執行本機工作台 | [Python](https://www.python.org/downloads/windows/) |
| NumPy、Pillow | 影像陣列與 PNG 處理 | [NumPy](https://numpy.org/)、[Pillow](https://python-pillow.github.io/) |
| SciPy、OpenCV | 對位、變形與過渡 | [SciPy](https://scipy.org/)、[OpenCV](https://opencv.org/) |
| FFmpeg／FFprobe | 拆幀、影片資訊與透明 WebM | [下載](https://ffmpeg.org/download.html)、[授權說明](https://ffmpeg.org/legal.html) |
| ComfyUI | 外部模型推論服務 | [ComfyUI](https://github.com/Comfy-Org/ComfyUI) |
| Qwen-Image 2.1 | 預設逐幀去背模型 | [原模型](https://huggingface.co/Qwen/Qwen-Image-2.1)、[ComfyUI 格式與放置位置](https://huggingface.co/Comfy-Org/Qwen-Image-2.1) |

FFmpeg 的授權取決於實際編譯內容。若自行製作附帶第三方執行檔的套件，需另外處理該版本的授權與再散布義務。

## 工作流與 ComfyUI 節點

`qwen_original.json` 是供此工作台讀取的 API 工作流設定，已移除個人檔名與輸出路徑。它不包含模型實作或 ComfyUI 節點程式；也不是能任意替換節點編號的通用工作流介面。

本次已在 ComfyUI 官方原始碼確認以下節點：

- `QwenImage21Cache`、`TextEncodeQwenImage21`：[nodes_qwen.py](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_qwen.py)
- `SaveImageAdvanced`：[nodes_images.py](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_images.py)
- `ComfySwitchNode`：[nodes_logic.py](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_logic.py)

先更新至包含上述節點的 ComfyUI，再執行工作台的「檢查環境」。同名第三方節點不一定具有相同輸入，不能只憑名稱判定相容。這次沒有驗證所有 ComfyUI 發行版本，因此不承諾一個通用最低版本。

分享包內的三個模型檔名與官方 ComfyUI 重打包文件相符；這不代表已核對任何使用者電腦上模型檔的下載來源或雜湊。詳見 [完整依賴與限制](依賴與限制.txt)。

## v3.11 本地接縫重建

`rife_network.py` 取自官方 RIFE 4.25 模型包的 IFNet 架構，`rife_warp.py` 取自 [Practical-RIFE](https://github.com/hzwer/Practical-RIFE) 的 warplayer；保留 [RIFE_LICENSE.txt](RIFE_LICENSE.txt)。調整僅為本機 import 路徑。模型來源與 SHA-256 固定於 `setup_seam_model.py`，權重不隨分享包散布。上游程式與模型授權仍依上游條文，工作台的 MIT 不替第三方重新授權。PyTorch 在獨立 Python 執行，不修改 ComfyUI 環境。
