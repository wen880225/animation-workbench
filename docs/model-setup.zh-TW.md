# v3.12 本地接縫模型安裝

本節只用於「一鍵修復全部接點」。去背仍由 ComfyUI 執行。分享包不包含 PyTorch、CUDA 執行環境或模型權重；setup.cmd 只安裝工作臺基本依賴。

## 1. 準備獨立的 Python 環境

目前接縫模型實作需要 NVIDIA GPU 與 CUDA 可用的 PyTorch；CPU、AMD、Apple GPU 沒有此版可用的推論路徑。已驗證環境為 Windows、Python 3.12、RTX 3080 12 GB、PyTorch 2.10.0+cu130；這是測試配置，不是最低規格保證。

在工具資料夾用 PowerShell 建立獨立環境（不修改 ComfyUI）：

```powershell
py -3.12 -m venv .rife-venv
.\.rife-venv\Scripts\python.exe -m pip install numpy==2.3.5 Pillow==10.4.0
```

到 [PyTorch 官方安裝頁](https://pytorch.org/get-started/locally/)選 Windows、Pip、Python 與符合顯示卡驅動的 CUDA 版本。把官方指令中的 `pip3` 或 `pip` 換成 `.\.rife-venv\Scripts\python.exe -m pip` 再執行，確保安裝在這個環境。不要選 CPU 套件。若已有獨立、可用的 CUDA PyTorch Python，可沿用其完整路徑。

## 2. 驗證 CUDA，下載模型

```powershell
.\.rife-venv\Scripts\python.exe -c "import torch; print(torch.__version__); print(torch.cuda.is_available())"
.\.rife-venv\Scripts\python.exe setup_seam_model.py --python .\.rife-venv\Scripts\python.exe
```

第一行的 CUDA 結果應為 True。安裝腳本再次驗證 CUDA，再下載官方 RIFE 4.25 權重，核對固定 SHA-256，寫入 `models/rife425/flownet.pkl` 與本機 `repair_runtime.json`。成功時顯示 `Local seam model ready; ComfyUI unchanged.`。下載失敗或雜湊不符請檢查網路／來源，不要跳過驗證。

模型來源與實作說明：[Practical-RIFE](https://github.com/hzwer/Practical-RIFE)、[第三方紀錄](../THIRD_PARTY.md)。權重另下載，來源與固定雜湊可查 `setup_seam_model.py`。

## 3. 回到工作臺

啟動工作臺並確認 v3.12.0，加入已去背短片，選順序循環，按「一鍵修復全部接點」。成功後看「整段比例」狀態、慢播及完整路線，最後輸出。

- CUDA 為 False：核對 NVIDIA 驅動、官方安裝指令與實際 Python 環境。
- 缺 NumPy／Pillow：補裝至上述推論環境，不是 ComfyUI 的環境。
- VRAM 不足：結束不需要的 GPU 工作後再試；尚無 CPU 備援，不會偷偷縮小輸出尺寸。
- 搬動工具或 Python 目錄：重新執行安裝指令，更新 repair_runtime.json 的絕對路徑；權重已通過驗證時可重用。
- 失敗詳情：該次 `transition_projects/專案ID/repairs/版本/model.log`。失敗不取代原草稿。

不要把 `.rife-venv`、models、repair_runtime.json 或自己的素材一起重新打包分享。
