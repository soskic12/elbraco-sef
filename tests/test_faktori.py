"""Kada je izvedeni faktor pakovanja pouzdan.

Faktor množi količinu koja ulazi u ERP. Pogrešan faktor **tiho upiše pogrešnu
zalihu** i to se vidi tek kad proizvodnja ode u minus — kao `HAUSBRANDT HERMAN
1kg` koji je završio kao 6 grama umesto 6.000. Zato su pravila stroga.
"""

from __future__ import annotations

from collections import Counter

from sefsync.services.faktori import oceni


def test_vidjeno_dvaput_je_dovoljno():
    odluka = oceni(Counter({20.0: 2}), None)
    assert odluka is not None
    faktor, razlog = odluka
    assert faktor == 20.0
    assert "2 puta" in razlog


def test_vidjeno_jednom_vredi_samo_ako_naziv_kaze_isto():
    """Dva nezavisna izvora: istorija i naziv stavke."""
    assert oceni(Counter({40.0: 1}), None) is None
    assert oceni(Counter({40.0: 1}), 20.0) is None

    odluka = oceni(Counter({40.0: 1}), 40.0)
    assert odluka is not None and odluka[0] == 40.0
    assert "naziv" in odluka[1]


def test_kolebljiva_veza_se_ne_upisuje_nikad():
    """Mereno na razvojnoj bazi: 84061 je viđen ×1 (22 puta), ×1,5 i ×3.

    Takav faktor znači da je neko jednom uneo drugačije pakovanje; množiti po
    njemu bi pokvarilo 22 ispravna slučaja.
    """
    assert oceni(Counter({1.0: 22, 1.5: 1, 3.0: 1}), None) is None
    assert oceni(Counter({2.0: 2, 1.0: 1}), 2.0) is None


def test_faktor_jedan_se_ne_upisuje():
    """Ne menja ništa, a zauzeo bi mesto i izgledao kao odluka."""
    assert oceni(Counter({1.0: 50}), 1.0) is None


def test_prazno_nista_ne_daje():
    assert oceni(Counter(), None) is None
