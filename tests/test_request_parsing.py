"""Small shared parsers: form_flag (app.request_parsing) and the file
extension helpers in app.media.processing."""
import pytest

from app.media.processing import allowed_ext, allowed_file, ext_type, file_type
from app.request_parsing import form_flag


@pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'on', 'On', 'yes', ' yes ', True, 1])
def test_form_flag_on(value):
    assert form_flag(value) is True


@pytest.mark.parametrize('value', [None, '', '0', 'false', 'off', 'no', 'checked', False, 0])
def test_form_flag_off(value):
    assert form_flag(value) is False


@pytest.mark.parametrize('ext, ftype', [
    ('png', 'image'), ('JPG', 'image'), ('gif', 'gif'), ('mp4', 'video'),
    ('WebM', 'video'), ('exe', 'unknown'), ('', 'unknown'),
])
def test_the_extension_helpers_agree_with_the_name_based_ones(ext, ftype):
    assert ext_type(ext) == ftype == file_type(f'clip.{ext}')
    assert allowed_ext(ext) == (ftype != 'unknown') == allowed_file(f'clip.{ext}')
