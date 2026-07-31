"""C1 fidelity: does the produced tool actually implement the brief?"""

from conftest import record


def test_basic_totals_sorted_ascending(run, ledger):
    body = "\n".join([
        record("123.26", "transit", "275", "bus"),
        record("123.26", "groceries", "450", "corner store"),
        record("124.26", "groceries", "1299", "weekly shop"),
    ]) + "\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "groceries\t17.49\ntransit\t2.75\nrejected\t0\n"


def test_trailing_minus_is_a_negative_amount(run, ledger):
    body = "\n".join([
        record("123.26", "transit", "275", "bus"),
        record("124.26", "transit", "400-", "refund"),
    ]) + "\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "transit\t-1.25\nrejected\t0\n"


def test_leading_minus_is_rejected_not_negative(run, ledger):
    body = "\n".join([
        record("123.26", "transit", "275", "bus"),
        "124.26|transit|-400|leading minus|x",
    ]) + "\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "transit\t2.75\nrejected\t1\n"


def test_escaped_pipe_in_memo_does_not_split_fields(run, ledger):
    body = record("126.26", "misc", "7", r"note with \p pipe inside") + "\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "misc\t0.07\nrejected\t0\n"


def test_escaped_backslash_in_memo(run, ledger):
    body = record("126.26", "misc", "100", r"path C:\\temp") + "\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "misc\t1.00\nrejected\t0\n"


def test_bad_escape_sequence_is_rejected(run, ledger):
    line = r"126.26|misc|100|bad \x escape|"
    body = line + "0\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rejected\t1\n"


def test_bad_check_character_is_rejected(run, ledger):
    good = record("123.26", "groceries", "450", "ok")
    bad = good[:-1] + ("z" if good[-1] != "z" else "y")
    result = run(ledger(good + "\n" + bad + "\n"))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "groceries\t4.50\nrejected\t1\n"


def test_comments_and_blank_lines_are_ignored_not_rejected(run, ledger):
    body = (
        "# a comment\n"
        "\n"
        "   \n"
        + record("123.26", "rent", "150000", "月租") + "\n"
    )
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rent\t1500.00\nrejected\t0\n"


def test_zero_sum_category_is_still_printed(run, ledger):
    body = "\n".join([
        record("123.26", "wash", "500", "paid"),
        record("124.26", "wash", "500-", "refunded"),
    ]) + "\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "wash\t0.00\nrejected\t0\n"


def test_leading_zero_amount_is_valid(run, ledger):
    body = record("366.26", "misc", "0450", "leading zeros") + "\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "misc\t4.50\nrejected\t0\n"


def test_no_thousands_separator(run, ledger):
    body = record("1.26", "rent", "123456789", "big") + "\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rent\t1234567.89\nrejected\t0\n"


def test_day_zero_and_day_367_are_rejected(run, ledger):
    body = "0.26|misc|1|day zero|x\n367.26|misc|1|day 367|x\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rejected\t2\n"


def test_wrong_field_count_is_rejected(run, ledger):
    body = "123.26|misc|450|only four fields\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rejected\t1\n"


def test_uppercase_category_is_rejected(run, ledger):
    body = "123.26|Groceries|450|uppercase|x\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rejected\t1\n"


def test_decimal_point_amount_is_rejected(run, ledger):
    body = "123.26|groceries|4.50|decimal point|x\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rejected\t1\n"


def test_empty_file(run, ledger):
    result = run(ledger(""))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rejected\t0\n"


def test_missing_file_exits_2(run, tmp_path):
    result = run(str(tmp_path / "does-not-exist.ldg"))
    assert result.returncode == 2
    assert result.stderr.strip() != ""


def test_rejected_records_do_not_abort_the_file(run, ledger):
    body = "\n".join([
        record("123.26", "groceries", "450", "first"),
        "garbage line that is not a record at all",
        record("124.26", "groceries", "550", "after the garbage"),
    ]) + "\n"
    result = run(ledger(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "groceries\t10.00\nrejected\t1\n"
