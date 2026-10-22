"""app.files.remove_if_exists: the one "delete it if it is there"."""
import os

import pytest

from app.files import remove_if_exists


def test_removes_a_file_and_says_so(tmp_path):
    f = tmp_path / 'a.png'
    f.write_bytes(b'x')
    assert remove_if_exists(str(f)) is True
    assert not f.exists()


def test_a_missing_file_is_not_an_error(tmp_path):
    assert remove_if_exists(str(tmp_path / 'gone.png')) is False


@pytest.mark.parametrize('path', [None, ''])
def test_an_empty_path_is_ignored(path):
    assert remove_if_exists(path) is False


def test_a_directory_still_raises(tmp_path):
    """Only a missing file is forgiven; anything else is a real problem."""
    d = tmp_path / 'dir'
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        remove_if_exists(str(d))
    assert os.path.isdir(d)
