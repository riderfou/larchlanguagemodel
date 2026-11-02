# Météo des mélèzes 🌲→🟡

Une « météo des couleurs d'automne » pour les mélèzes : l'app relève des
webcams de montagne plusieurs fois par jour, mesure la part d'aiguilles
jaunes/orangées dans les zones de mélèzes, et publie une page récapitulative
avec, pour chaque webcam :

- le **stade** : encore vert · début de coloration · coloration en cours · pic · déclin ;
- l'**indice d'or** (0 = tout vert, 100 = tout doré) et la couleur moyenne des aiguilles ;
- la **date estimée du pic** et la tendance à J+3 / J+7 / J+14 ;
- la courbe de la saison et, au survol, l'image de classification des pixels.

## Démarrage

```bash
pip install -e '.[browser,dev]'
```

```bash
playwright install chromium
```

```bash
larch run
```

La page est générée dans `site/index.html` (et `site/latest.json`).

Autres commandes :

| Commande | Rôle |
|---|---|
| `larch fetch` | relève et analyse les images, ajoute à `data/history.csv` |
| `larch report` | (re)génère la page à partir de l'historique |
| `larch analyze photo.jpg --cam saint-veran --overlay out.png` | analyse une image locale |
| `larch roi saint-veran` | image quadrillée pour tracer les zones de mélèzes |
| `larch calibrate` | ajuste le modèle de prévision sur les pics observés |

## Configurer les webcams (`cams.yaml`)

Dix webcams sont préconfigurées : Bessans, Serre Chevalier – La Méa,
Saint-Véran, et une sélection Skaping où des mélézins sont bien visibles
(Vars ×3, Monêtier, Les Menuires, Chamonix-Lognan, Valloire). Pour chacune :

1. **La source.**
   - `type: skaping` : n'importe quelle page `www.skaping.com/<lieu>/<vue>`.
     La dernière image est lue dans le HTML, sans navigateur, en 4096 px
     (`quality: large`). La liste de toutes les webcams Skaping est sur
     https://www.skaping.com/camera/mosaic.
   - `type: webcamhd` : webcams app.webcam-hd.com (Bessans), via leur API.
   - `type: image` : URL directe d'un JPEG (outils développeur → onglet Réseau
     → filtrer `jpg`), le plus simple quand elle existe.
   - `type: browser` : Chromium headless, plus grande image visible de la page.
     Dernier recours (Saint-Véran, dont les lecteurs viennent de
     vision-environnement.com).
   - `type: windy` : webcams Windy (clé gratuite sur https://api.windy.com/keys
     dans la variable `WINDY_API_KEY`).

   Domaines contactés : `www.skaping.com`, `skaping.s3.gra.io.cloud.ovh.net`
   (secours : `rvfbdt.skaping.com`), `app.webcam-hd.com`,
   `skol-webcams-archives.s3.fr-par.scw.cloud`, `www.vision-environnement.com`,
   `api.open-meteo.com`.
2. **La zone de mélèzes (ROI).** Indispensable pour une mesure juste : sans
   ROI, l'herbe jaunie, les épicéas ou les pins cembro faussent l'indice.
   `larch roi <id>` produit une image quadrillée (coordonnées 0–1) ; reportez les
   polygones dans `roi:`. Survoler l'image dans la page montre ce qui est compté
   (vert / jaune / orange / brun).
3. **Position et altitude** des mélèzes visibles (pour la météo). Les valeurs
   fournies sont approximatives.

## Comment c'est mesuré

Dans la ROI, on écarte ciel, neige, rochers et nuages (pixels peu saturés),
ombres et nuit (trop sombres), puis on classe les pixels par teinte :
vert (64–170°), jaune d'or (40–64°), orangé (18–40°) ; les teintes chaudes mais
sombres/ternes sont comptées comme « brun » (aiguilles sèches, sol, troncs).

- **Indice d'or** = (jaune + orangé) / (vert + jaune + orangé).
- On garde aussi les coordonnées chromatiques GCC/RCC (méthode PhenoCam).
- Images de nuit, de brouillard ou sans végétation : marquées invalides.
- Valeur du jour = médiane des images prises entre 10 h et 15 h ; la courbe est
  lissée (médiane sur 3 jours) pour gommer nuages et reflets.
- **Déclin** : l'indice baisse nettement après un pic, ou la végétation colorée
  disparaît (les aiguilles tombent, le mélèze devient gris).

## Prévision du pic

Trois sources, la plus fiable disponible est affichée :

1. **Pic observé** : une fois le déclin amorcé, date du maximum de l'indice.
2. **D'après l'évolution observée** : dès que la coloration a commencé
   (≥ 5 jours de mesures, +15 points), une courbe logistique est ajustée ;
   le pic est la date où 90 % de la coloration est atteinte, avec une
   fourchette.
3. **Modèle « froid cumulé »** (avant le début de la coloration) : à partir du
   1er août, on cumule les degrés-jours sous 10 °C (températures Open-Meteo à
   l'altitude des mélèzes, observées puis prévues à 16 jours, puis prolongées par
   une tendance saisonnière) ; le pic arrive quand le cumul atteint un seuil.
   **Le seuil par défaut (45) est une estimation grossière** : après une saison
   d'observations, `larch calibrate` le recale pour chaque webcam
   (`data/calibration.json`), et la prévision s'améliore d'année en année.

## Automatisation (GitHub Actions + GitHub Pages)

`.github/workflows/meteo.yml` lance `larch run` trois fois par jour de
septembre à novembre, sauvegarde l'historique dans `data/` (une image archivée
par webcam et par jour) et publie la page sur GitHub Pages.
À faire une fois : *Settings → Pages → Source : GitHub Actions*, et, pour
Windy, ajouter le secret `WINDY_API_KEY`.

## Pistes

- Images satellite Sentinel-2 (10 m, tous les ~5 jours) pour couvrir des
  versants entiers sans webcam : indice de jaunissement sur des masques de
  mélézins (carte forestière IGN BD Forêt).
- Notifications (« Saint-Véran passe au pic ! »).
- Autres essences (hêtres, érables, bouleaux) avec leurs propres teintes.
