import pytest

from rlworld.actions.geometry import ScreenGeometry, fit_within, smart_resize


def test_fit_within_keeps_the_aspect_ratio_and_never_upscales():
    assert fit_within(1920, 1080, 1280, 800) == (1280, 720)
    assert fit_within(1024, 768, 1280, 800) == (1024, 768)
    assert fit_within(1080, 1920, 1280, 800) == (450, 800)


def test_smart_resize_rounds_to_the_patch_factor():
    assert smart_resize(1920, 1080) == (1932, 1092)
    assert smart_resize(1932, 1092) == (1932, 1092)


def test_smart_resize_respects_pixel_bounds():
    width, height = smart_resize(3840, 2160, max_pixels=1280 * 28 * 28)
    assert width % 28 == 0 and height % 28 == 0
    assert width * height <= 1280 * 28 * 28
    tiny_width, tiny_height = smart_resize(20, 20)
    assert tiny_width * tiny_height >= 56 * 56


def test_scaling_both_ways():
    geometry = ScreenGeometry(env_width=1920, env_height=1080, model_width=1280, model_height=720)
    assert geometry.to_env(640, 360) == (960, 540)
    assert geometry.to_model(960, 540) == (640, 360)
    assert not geometry.is_identity


def test_coordinates_clamp_to_the_screen():
    geometry = ScreenGeometry(env_width=1920, env_height=1080, model_width=1280, model_height=720)
    assert geometry.to_env(-50, 5000) == (0, 1079)
    assert geometry.to_model(1919, 1079) == (1279, 719)


def test_round_trips_stay_within_one_pixel():
    geometry = ScreenGeometry(env_width=1920, env_height=1080, model_width=1932, model_height=1092)
    for env_x, env_y in [(0, 0), (17, 33), (959, 541), (1500, 1000), (1919, 1079)]:
        back_x, back_y = geometry.to_env(*geometry.to_model(env_x, env_y))
        assert abs(back_x - env_x) <= 1 and abs(back_y - env_y) <= 1


def test_sizes_must_be_positive():
    with pytest.raises(ValueError):
        ScreenGeometry(env_width=0, env_height=1080, model_width=1280, model_height=720)
