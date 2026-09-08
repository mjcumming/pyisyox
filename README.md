# pyisyox (mike2)

Private-maintenance fork of [shbatm/pyisyox](https://github.com/shbatm/pyisyox), recovered from a live Home Assistant install after the original GitHub repo was deleted.

This tree is the **mike2** branch used with ISY-994 firmware 5.x and the companion integration [mjcumming/hacs-isy994](https://github.com/mjcumming/hacs-isy994).

## Home Assistant

Production HA instances vendor this package under `/config/pyisyox` and pin:

```text
pyisyox @ file:///config/pyisyox
```

so Core updates do not clone GitHub. After changing this repo, copy the package into `/config/pyisyox` on each HA box (or rebuild that folder from the `1.0.0b0-mike2` tag).

## Install from Git

```bash
pip install "pyisyox @ git+https://github.com/mjcumming/pyisyox@1.0.0b0-mike2"
```
