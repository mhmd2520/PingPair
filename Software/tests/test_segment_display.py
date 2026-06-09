"""segment_display_name — collapses the "Segment N (Segment N)" doubling.

When the operator leaves the segment-label field blank it auto-defaults
to "Segment N"; the UI used to wrap that as "Segment N (Segment N)".
"""

from pingpair.views.segment_dialog import segment_display_name


def test_blank_label_is_plain_segment_n() -> None:
    assert segment_display_name(3, "") == "Segment 3"
    assert segment_display_name(3, "   ") == "Segment 3"


def test_default_label_collapses_no_doubling() -> None:
    # The auto-default label is "Segment N" — must NOT render as
    # "Segment 1 (Segment 1)".
    assert segment_display_name(1, "Segment 1") == "Segment 1"


def test_custom_label_is_parenthesised() -> None:
    assert segment_display_name(2, "Cab M2-M4") == "Segment 2 (Cab M2-M4)"


def test_custom_label_is_trimmed() -> None:
    assert segment_display_name(2, "  Cab M2-M4  ") == "Segment 2 (Cab M2-M4)"
