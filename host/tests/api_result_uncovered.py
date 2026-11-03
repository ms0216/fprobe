"""試験から呼べない（応答を照合できない）API のメソッドと、その理由（tests/api_result_check.py が使う）。

呼べるようになったら消す（照合されたのに残っていれば全体の試験が失敗する）。
"""

UNCOVERED: dict[str, str] = {
}
