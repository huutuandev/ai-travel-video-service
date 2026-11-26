from time import monotonic

import pytest

from app.core.config import settings
from app.services.tts import NarrationError, check_model, synthesis_slot


def test_missing_model_reports_setup(tmp_path):
    with pytest.raises(NarrationError, match="setup_tts_model.py") as error:
        check_model(tmp_path)
    assert error.value.code == "TTS_MODEL_UNAVAILABLE"


def test_shared_media_slot_is_bounded(configuration, tmp_path):
    config = settings().model_copy(update={"media_root": tmp_path, "tts_max_concurrent": 1})
    with synthesis_slot(config, monotonic() + 1):
        with pytest.raises(NarrationError) as error:
            with synthesis_slot(config, monotonic() + 0.03):
                pytest.fail("Two jobs acquired the single synthesis slot")
    assert error.value.code == "TTS_TIMEOUT"
    with synthesis_slot(config, monotonic() + 1):
        pass

