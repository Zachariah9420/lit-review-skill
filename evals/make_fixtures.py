# -*- coding: utf-8 -*-
"""產生迴歸測試用的凍結 fixture(不需網路)。改動 fixture 請改這支再重跑。"""
import json
import os
import zipfile

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
os.makedirs(BASE, exist_ok=True)


def w(name, content, mode="w"):
    path = os.path.join(BASE, name)
    with open(path, mode, encoding=None if "b" in mode else "utf-8") as f:
        f.write(content)
    return path


# --- JSON fixtures ---
w("not_json.txt", "this is not json at all\n")
w("list_root.json", json.dumps([
    {"title": "A Paper", "year": 2024, "authors": ["A. One"], "doi": "10.1/a",
     "abstract": "Some abstract text."},
], ensure_ascii=False, indent=1))
w("single_work.json", json.dumps(
    {"source": "crossref", "title": "A Single Work Record", "year": 2023,
     "authors": ["Solo, Author"], "doi": "10.1/single", "container": "Journal of Tests",
     "type": "journal-article", "volume": "1", "issue": "2", "page": "3-4"},
    ensure_ascii=False, indent=1))
w("two_works.json", json.dumps({"results": [
    {"title": "First Work", "year": 2020, "authors": ["A. One"], "doi": "10.1/1"},
    {"title": "Second Work", "year": 2021, "authors": ["B. Two"], "doi": "10.1/2"},
]}, ensure_ascii=False, indent=1))

# --- cite_integrity fixtures ---
w("apa_style.md", "# Draft\n\n研究顯示此現象存在 (Smith, 2020)，後續亦有支持 "
                  "(Lee et al., 2019)。\n\n## References\n\n"
                  "Smith, J. (2020). A title. Journal of Things.\n"
                  "Lee, K., et al. (2019). Another title. Other Journal.\n")
w("big_range.md", "# Draft\n\n綜合前述研究 [1-52] 可知此結論成立。\n\n"
                  "## References\n\n" + "".join(f"[{i}] Author {i}. Title {i}.\n"
                                                for i in range(1, 53)))
w("heading_in_title.md", "# Draft\n\n本文引用 [1] 與 [2]。\n\n## References\n\n"
                         "[1] A. Author, \"References in science: a survey,\" Journal, 2020.\n"
                         "[2] B. Author, \"Another work,\" Journal, 2021.\n")
w("ranges.md", "# Draft\n\n見 [8-10] 與 [1, 3-5]。\n\n## References\n\n"
               + "".join(f"[{i}] Author {i}. Title.\n" for i in range(1, 11)))
w("dup_refs.md", "# Draft\n\n引用 [1] [2] [3]。\n\n## References\n\n"
                 "[1] A.\n[2] B.\n[3] C.\n[3] C duplicate.\n")
w("overcite.md", "# Draft\n\n引用 [1] [2] 以及不存在的 [100]。\n\n## References\n\n"
                 + "".join(f"[{i}] Author {i}.\n" for i in range(1, 11)))
w("fake.docx", "this is plain text pretending to be a docx\n")

# --- 真 docx：內含 XML 字元參照(Word 就是這樣寫 en-dash 與引號的)---
DOC_XML = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>'
           '<w:p><w:r><w:t>Draft with &quot;quoted&quot; text</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>綜合研究 [8&#x2013;10] 可知結論成立。</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>參考文獻</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>[8] Author Eight. Title.</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>[9] Author Nine. Title.</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>[10] Author Ten. Title.</w:t></w:r></w:p>'
           '</w:body></w:document>')
with zipfile.ZipFile(os.path.join(BASE, "charrefs.docx"), "w") as z:
    z.writestr("[Content_Types].xml",
               '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxml'
               'formats.org/package/2006/content-types"><Default Extension="xml" '
               'ContentType="application/xml"/></Types>')
    z.writestr("word/document.xml", DOC_XML)

print("fixtures written to", BASE)
for n in sorted(os.listdir(BASE)):
    print(" ", n)
