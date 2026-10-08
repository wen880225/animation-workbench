# 動畫去背工作臺 · Animation Workbench

專為遊戲角色動畫收尾：在 Windows 本機瀏覽器中批次去背，安排單段循環或多段順序，一鍵重建接縫並播放驗收，再另存透明 PNG 圖序與 WebM。

![v3.12 一鍵修復與整段比例穩定，使用合成機器人](docs/images/scale-stability-312.jpg)

## 下載與開始

- **[下載公開發布版](https://github.com/wen880225/animation-workbench/releases/latest)**：**v3.12.0**，請下載 Release 附件 `AnimationWorkbench_v3.12.0_source.zip`，完整解壓縮後安裝。
- **[新手教學：安裝到第一次輸出](docs/getting-started.zh-TW.md)**：包含環境檢查、接點修整、例外檢查與排錯。
- [完整使用說明](使用說明.txt) · [依賴與限制](依賴與限制.txt)
- [更新紀錄](CHANGELOG.md) · [v3.10.3 預覽操作修正](docs/v3.10.3-validation.zh-TW.md) · [v3.10.2 局部對位修正](docs/v3.10.2-validation.zh-TW.md) · [v3.10.1 修正與驗收](docs/v3.10.1-validation.zh-TW.md) · [v3.10 驗收與升級說明](docs/v3.10-validation.zh-TW.md) · [相近工具比較](docs/similar-tools.zh-TW.md)

**這是原始碼套件，並非免安裝 EXE。** 需要 Windows x64、Python 3.12、FFmpeg／FFprobe，以及包含所需節點的本機 ComfyUI 與另行下載的模型。`setup.cmd` 只安裝工作臺的 Python 套件，不會安裝 ComfyUI、下載模型或處理顯示卡驅動。尚未完成不同硬體與乾淨 Windows 的全面驗證；請先閱讀教學中的環境要求。

**工作臺的 MIT 授權不涵蓋模型。** 隨附工作流使用的 Qwen Image 2.1 採非商業研究／評估授權，商業使用需另向官方取得授權，詳見[官方條文](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE)與[第三方說明](THIRD_PARTY.md)。

## 可以完成什麼

- 一次匯入多支影片，依序去背；可先試跑少量影格再確認整段。
- 以「素材檢查」查看播放與逐幀細節；單段與多段都在「接點修整」完成。
- 明確選擇循環或依序播完，批次加入素材並排列順序。
- 一鍵先穩定整段微小比例漂移，再重建單段循環及 A 尾幀 → B 首幀的兩側過渡；完成後直接套用，可復原。
- 本地 RIFE 4.25 估計運動，RGBA 共用位移，保留原解析度；比較、慢播與輸出共用修復 PNG。
- 草稿路線可先預覽；輸出後載入該次實際 PNG 成品，保留來源及歷史版本。

v3.10 整併正常操作入口，舊單段複雜方案保留在相容入口，不會默默轉換。**儲存方案保存設定，按輸出才會產生成品。** 自動分析使用本機演算法，沒有語義 AI 自動驗收。明暗統一不等於去閃爍；端點圖相同也不保證動作自然。輸出無音軌。v3.11 新增 RIFE 接縫重建；模型權重另裝，詳見下方說明。

## 授權

工作臺自身程式碼採用 [MIT License](LICENSE)，允許依其條款使用、修改與商用。ComfyUI、FFmpeg、節點及各模型適用各自條款，工作臺的 MIT 授權不會替代它們；隨附工作流的來源與再散布權亦需分別確認。詳見[第三方說明](THIRD_PARTY.md)及[依賴與限制](依賴與限制.txt)。套件不包含模型權重、第三方執行檔或使用者素材。

## v3.11 一鍵修復

[操作、模型安裝、驗證與限制](docs/v3.11-repair.zh-TW.md)。新電腦需要另裝 NVIDIA CUDA 可用的 PyTorch 與 RIFE 權重，見[模型安裝](docs/model-setup.zh-TW.md)；分享原始碼不包含模型或個人 Python 路徑。

## v3.12 整段比例穩定

原本一鍵修復會先穩定整段比例，再重建接點，減少尾段集中縮回。詳見 [操作、驗證與限制](docs/v3.12-stability.zh-TW.md)。舊方案請重新修復；固定解析度與原素材保留。

## 驗證狀態

v3.12 的 0.5%、1%、2% 合成比例漂移，以及單段循環／A→B→C 的實際 RIFE、PNG 與透明 WebM 輸出已驗證。專案維護者已完成單表情循環實測並認可效果；**多表情實際素材驗收仍在進行**。這不是對所有生成動作的品質保證，詳見 [v3.12 驗證紀錄](docs/v3.12-stability.zh-TW.md)。

## 貢獻者

- **[wen880225](https://github.com/wen880225)**：專案維護、需求與工作流程設計、素材實測及最終驗收。
- **Codex（OpenAI AI coding assistant）**：協助程式實作、除錯、演算法整合、合成測試、介面與文件整理。

完整說明見 [CONTRIBUTORS.md](CONTRIBUTORS.md)。AI 協助署名不表示 OpenAI 官方維護或背書本專案。
