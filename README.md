# 動畫去背工作臺 · Animation Workbench

在 Windows 本機瀏覽器中，將影片逐幀去背、修整單段循環，再把多段動畫排成一組，另存透明 PNG 圖序與 WebM。

![動畫去背工作臺介面，使用中性示範素材](docs/images/workbench.jpg)

## 下載與開始

- **[下載最新發布版](https://github.com/wen880225/animation-workbench/releases/latest)**：在 Assets 中選擇 `AnimationWorkbench_v3.9_source.zip`。
- **[新手教學：安裝到第一次輸出](docs/getting-started.zh-TW.md)**：包含環境檢查、單段修整、多段四步流程與排錯。
- [完整使用說明](使用說明.txt) · [依賴與限制](依賴與限制.txt)
- [更新紀錄](CHANGELOG.md) · [相近工具比較](docs/similar-tools.zh-TW.md)

**這是原始碼套件，並非免安裝 EXE。** 需要 Windows x64、Python 3.12、FFmpeg／FFprobe，以及包含所需節點的本機 ComfyUI 與另行下載的模型。`setup.cmd` 只安裝工作臺的 Python 套件，不會安裝 ComfyUI、下載模型或處理顯示卡驅動。尚未完成不同硬體與乾淨 Windows 的全面驗證；請先閱讀教學中的環境要求。

**工作臺的 MIT 授權不涵蓋模型。** 隨附工作流使用的 Qwen Image 2.1 採非商業研究／評估授權，商業使用需另向官方取得授權，詳見[官方條文](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE)與[第三方說明](THIRD_PARTY.md)。

## 可以完成什麼

- 一次匯入多支影片，依序去背；可先試跑少量影格再確認整段。
- 檢查播放與逐幀細節，修整一段動畫的明暗及首尾循環。
- 依「素材與順序 → 統一明暗 → 修好接點 → 預覽與輸出」串接多段動畫。
- 保留原始去背圖，將修整結果另存成版本；載入實際輸出 PNG 檢查成品。

v3.9 整理了新手導覽、四步操作、大字與配色設定，並改善單段預覽的可用空間與縮放顯示。**儲存方案保存設定，按輸出才會產生成品。** 明暗統一不等於去閃爍；首尾或接點圖相同，也不保證動作、速度與節奏自然。輸出無音軌，未內建 RIFE。

## 授權

工作臺自身程式碼採用 [MIT License](LICENSE)，允許依其條款使用、修改與商用。ComfyUI、FFmpeg、節點及各模型適用各自條款，工作臺的 MIT 授權不會替代它們；隨附工作流的來源與再散布權亦需分別確認。詳見[第三方說明](THIRD_PARTY.md)及[依賴與限制](依賴與限制.txt)。套件不包含模型權重、第三方執行檔或使用者素材。
