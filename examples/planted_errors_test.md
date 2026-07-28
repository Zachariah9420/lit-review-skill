# 測試稿:第二章節錄(LLM 維修 SOP 生成之幻覺問題)

大型語言模型雖能流暢生成維修程序,但其輸出存在幻覺(hallucination)問題,可能生成看似合理但實際錯誤的步驟 [1]。近年研究顯示,透過取樣一致性可在不依賴外部資料庫的情況下偵測幻覺 [2]。Transformer 架構自提出以來已成為此類生成模型的基礎 [3]。基於本體論的故障診斷方法已被應用於智慧售票設備 [4]。此外,知識圖譜與本體論已被廣泛用於工業維修領域的知識管理,惟此主張目前尚缺乏引用支持。國內亦有研究探討智慧型維修決策支援系統 [5]。

## 參考文獻

[1] Ji, Z., Lee, N., Frieske, R., Yu, T., Su, D., Xu, Y., Ishii, E., Bang, Y., Madotto, A., & Fung, P. (2023). Survey of hallucination in natural language generation. ACM Computing Surveys, 55(12), 1–38.

[2] Manakul, P., Liusie, A., & Gales, M. (2022). SelfCheckGPT: Zero-resource black-box hallucination detection for generative large language models. In Proceedings of the 60th Annual Meeting of the Association for Computational Linguistics (ACL 2022).

[3] Vaswani, A., & Shazeer, N. (2019). Attention is all you need. Journal of Machine Learning Research, 20(1), 1–15.

[4] Chen, L., & Wang, H. (2021). Ontology-driven fault diagnosis for smart ticket vending machines using large language models. IEEE Transactions on Industrial Informatics, 17(8), 5555–5567.

[5] 林大明(2020)。智慧型維修決策支援系統之研究。台灣工程學刊,36(2),45–60。
