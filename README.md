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

La page est générée dans `site/index.html` (et `site/latest.json`), avec la
carte des mélézins dans `site/carte.html`.

Autres commandes :

| Commande | Rôle |
|---|---|
| `larch fetch` | relève et analyse les images, ajoute à `data/history.csv` |
| `larch report` | (re)génère la page à partir de l'historique |
| `larch analyze photo.jpg --cam saint-veran --overlay out.png` | analyse une image locale |
| `larch roi saint-veran` | image quadrillée pour tracer les zones de mélèzes |
| `larch calibrate` | ajuste le modèle de prévision sur les pics observés |
| `larch foret` | (re)télécharge les mélézins de la BD Forêt V2 pour la carte |
| `larch satellite` | cartographie les mélézins par Sentinel-2 (~1 h, une fois par automne) |
| `larch satsuivi` | indice d'or Sentinel-2 des mélézins à chaque passage (webcams et massifs) |

## Configurer les webcams (`cams.yaml`)

Neuf webcams sont préconfigurées, avec leurs zones de mélèzes (ROI) :
Bessans, Serre Chevalier – La Méa, Saint-Véran, et une sélection Skaping où
des mélézins sont bien visibles (Vars ×3, Monêtier, Les Menuires,
Chamonix-Lognan). Pour chacune :

1. **La source.**
   - `type: skaping` : n'importe quelle page `www.skaping.com/<lieu>/<vue>`.
     La dernière image est lue dans le HTML, sans navigateur, en 4096 px
     (`quality: large`). La liste de toutes les webcams Skaping est sur
     https://www.skaping.com/camera/mosaic.
   - `type: webcamhd` : webcams app.webcam-hd.com (Bessans), via leur API.
   - `type: image` : URL directe d'un JPEG (outils développeur → onglet Réseau
     → filtrer `jpg`), le plus simple quand elle existe.
   - `type: browser` : Chromium headless, plus grande image visible de la page.
     Dernier recours. (Les lecteurs vision-environnement.com, comme celui de
     Saint-Véran, sont des flux vidéo ; ils publient aussi une capture fixe
     `…/live/modules/timelapse/capture/<nom>.jpg`, utilisable en `type: image`.)
   - `type: windy` : webcams Windy (clé gratuite sur https://api.windy.com/keys
     dans la variable `WINDY_API_KEY`).

   Domaines contactés : `www.skaping.com`, `skaping.s3.gra.io.cloud.ovh.net`
   (secours : `rvfbdt.skaping.com`), `app.webcam-hd.com`,
   `skol-webcams-archives.s3.fr-par.scw.cloud`, `www.vision-environnement.com`,
   `api.open-meteo.com`, et pour la carte `data.geopf.fr` (IGN) et
   `cdnjs.cloudflare.com` (Leaflet).
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

## Carte des mélézins

`site/carte.html` (lien en haut de la page principale) est une carte Leaflet :

- **Mélèze pur** (teinte franche) : forêts fermées de mélèze pur de la
  **BD Forêt® V2** de l'IGN (code TFV `FF2-63-63`, au moins 75 % de mélèze dans
  le couvert), soit environ 4 800 polygones et 99 000 ha en France, des Alpes du
  Sud surtout. Clic sur un polygone : surface.
- **Conifères d'altitude, mélèze possible** (teintes claires) : dans les Alpes,
  au-dessus de 1 400 m, les formations de conifères dont la BD Forêt ne précise
  pas l'essence : mélange de conifères (`FF2-00-00`, `FF2-90-90`, îlots
  `FF2-00`) et forêt ouverte de conifères (`FO2`, souvent des mélézins clairs
  pâturés). Environ 9 400 polygones et 122 000 ha. L'altitude est celle du
  centre du polygone (RGE ALTI, service d'altimétrie IGN). À l'étage subalpin,
  ce sont souvent des mélézins mêlés de pin cembro, de pin à crochets ou
  d'épicéa, mais rien ne garantit la présence du mélèze.
- **Webcams** : couleur du stade, et au clic la dernière image, l'indice d'or,
  la date du pic et les surfaces de mélèze pur et de conifères d'altitude à
  moins de 5 km.
- **Fonds IGN** (Géoplateforme, sans clé) : Plan IGN, photographies aériennes,
  ombrage du relief et, pour le contexte, toutes les formations de la BD Forêt V2.

Limites de la donnée : la BD Forêt V2 ne nomme le mélèze que dans les
peuplements purs. Les mélézins mélangés sont dans les teintes claires, sans
certitude. Certains mélézins sont même classés autrement : aux Menuires, la
forêt filmée par la webcam figure en « autre pin pur », « mélange de feuillus »
ou « forêt ouverte de feuillus ». Le mélèze perd ses aiguilles comme un
feuillu, ce qui peut tromper la photo-interprétation. Ces classes, trop larges,
ne sont pas reprises.

Les polygones sont téléchargés une fois par le WFS de la Géoplateforme
(`larch foret`, ~6 min ; `--only pur|melange`, `--min-elevation 1400`),
simplifiés à 8–10 m (la BD Forêt est photo-interprétée au 1/25 000) et
enregistrés dans `data/meleze_bdforet_v2.geojson` (7 Mo) et
`data/coniferes_altitude_bdforet_v2.geojson` (11 Mo). Sur le site, ils sont
servis en `.json` compressé (~2 Mo chacun), et les conifères d'altitude se
chargent après le mélèze pur. La BD Forêt V2 n'étant plus mise à jour, il n'y
a pas à relancer cette commande. Données IGN sous Licence Ouverte Etalab 2.0.

## Mélézins détectés par satellite (Sentinel-2)

La BD Forêt passe à côté de nombreux mélézins (mélanges, erreurs de
photo-interprétation). `larch satellite` les cherche directement dans les
images Sentinel-2, en exploitant ce qui distingue le mélèze des autres
conifères : **vert en été, doré en octobre, puis sans aiguilles**.

1. **Images** : pour chacune des 7 tuiles Sentinel-2 des Alpes françaises
   (31TGJ, 31TGK, 31TGL, 31TGM, 32TLP, 32TLQ, 32TLR), une image d'été sans
   nuages (juillet-août) et les 4 images les plus claires de
   octobre-début novembre de l'année. Données L2A « collection 1 » en COG,
   lues directement sur le bucket public AWS (aucun compte), à 20 m. Nuages,
   ombres et neige sont écartés pixel par pixel (couche SCL).
2. **Variables** : réflectances d'été et médiane d'automne, NDVI, chute du NDVI
   et rougissement (rouge/vert) entre été et automne, indice de jaunissement
   maximal, altitude (Copernicus DEM), et moyennes locales sur 60 et 140 m
   (un mélézin est un peuplement, pas un pixel).
3. **Apprentissage** : la BD Forêt V2 fournit les exemples : mélèze pur contre
   sapin/épicéa, pins, feuillus, landes et prairies. Classifieur par gradient
   boosting (scikit-learn).
4. **Validation** : un bloc de 10 km sur quatre est écarté de l'apprentissage ;
   on y mesure précision et rappel dans les proportions réelles (le mélèze pur
   est très minoritaire), ce qui fixe le seuil de décision.
5. **Carte** : la prédiction est faite sur toute la forêt (masque « arbres »
   d'ESA WorldCover), les zones d'au moins 0,5 ha sont vectorisées dans
   `data/meleze_sentinel2.geojson` (couche orange de la carte).

**Résultats (automne 2026)** : 19 300 zones, environ **243 000 ha** de
mélèze détecté (contre 99 000 ha de mélèze pur dans la BD Forêt). Images
d'été du 7 juillet au 23 août, d'automne du 5 octobre au 1er novembre selon
les tuiles. Validation sur les blocs écartés, dans les proportions réelles :

| Seuil | Précision | Rappel | Fausses alarmes sapin/épicéa |
|---|---|---|---|
| **0,5 (retenu)** | **90 %** | **82 %** | 2,0 % |
| 0,7 | 95 % | 75 % | 0,8 % |
| 0,9 | 98,5 % | 59 % | 0,2 % |

Ces chiffres comparent à la BD Forêt, qui n'est pas une vérité parfaite. Une
pessière y compte jusqu'à 25 % de mélèze, donc une partie des « fausses
alarmes » est sans doute du vrai mélèze. À l'inverse, des mélézins clairs
ou peu étendus échappent au masque des arbres (WorldCover), et le satellite
peut manquer des peuplements que la BD Forêt connaît (autour de Bessans
par exemple). Les deux couches se complètent. Les tuiles débordent sur
l'Italie et la Suisse : la détection y est appliquée, mais sans exemples
locaux pour l'apprentissage. À contrôler en zoomant sur les photographies
aériennes.

Aux Menuires, il n'y a que de petits bosquets à la limite des arbres
(18 ha détectés à moins de 5 km, aucun dans la BD Forêt). Le mélézin filmé
par la webcam paraît dense de près, mais il est petit.

```bash
pip install -e '.[satellite]'
```

```bash
larch satellite --year 2026
```

Le cache (`.cache/sentinel2`, ~2 Go par tuile) permet de relancer
l'apprentissage sans tout retélécharger. Domaines contactés :
`e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com`,
`esa-worldcover.s3.eu-central-1.amazonaws.com`,
`copernicus-dem-30m.s3.amazonaws.com`, `data.geopf.fr`.

## Suivi de la coloration par satellite, en complément des webcams

`larch satsuivi` reprend la carte des mélézins (détection Sentinel-2 et mélèze
pur de la BD Forêt) comme masque. À chaque passage de Sentinel-2 (tous les
2 à 3 jours avec les trois satellites), il mesure la coloration dans deux sortes
de zones :

- **autour de chaque webcam**, sur le mélèze à moins de 4 km (`satellite.radius_km`) :
  la courbe satellite s'affiche sur la fiche de la webcam (carrés bleus), avec
  son stade et l'écart moyen entre webcam et satellite les jours communs, ce qui
  permet de contrôler l'une par l'autre ;
- **dans des massifs sans webcam** (`satellite.zones` dans `cams.yaml` : Clarée,
  Vallouise, Champsaur, Ubaye, Haut-Verdon, Haute-Tinée, Queyras, Haute-Maurienne,
  Haute-Tarentaise) : fiches « Massifs suivis par satellite » et losanges sur la
  carte.

**Indice d'or satellite.** Pour chaque pixel de mélèze sans nuage, ni neige, ni
ombre du relief, on mesure le jaunissement, c'est-à-dire la hausse de
(rouge − vert)/(rouge + vert) par rapport à une image d'été du même pixel.
Corrections et filtres :

- **Témoins.** On soustrait le jaunissement des conifères persistants voisins
  (épicéas, sapins, pins de la BD Forêt, à moins de 8 km, sur la même image). En
  octobre, le soleil bas et l'atmosphère rougissent toutes les forêts sur
  l'image ; sans cette correction, une pessière « jaunirait » presque autant qu'un
  mélézin.
- **Zéro de septembre.** La valeur médiane du 1er au 15 septembre, quand le
  mélèze est encore vert partout, est soustraite pour chaque zone. Elle vient de
  l'herbe qui sèche sous les mélézins clairs.
- **Échelle.** 100 correspond à un jaunissement de 0,12 en plus de celui des
  témoins, calé sur les courbes de 2026 (Vars, Clarée, Queyras…).
- **Chute des aiguilles.** Le NDVI du mélèze rapporté à l'été (et à celui des
  témoins) s'effondre quand les aiguilles tombent. En dessous de 40 % d'aiguilles,
  le jour compte comme « sans aiguilles », l'équivalent des images sans végétation
  colorée des webcams, ce qui déclenche le stade « déclin ».
- **Filtres.** On écarte une image quand moins de 20 % du mélèze est visible, en
  cas de voile léger (NDVI des témoins à moins de 95 % ou plus de 115 % de l'été,
  ou luminosité anormale), et pour un point isolé à plus de 30 points de ses
  voisins.

Les mesures vont dans `data/satellite_history.csv`, au même format que
l'historique des webcams. Stades, lissage, ajustement logistique et date du pic
sont donc calculés exactement de la même façon. Le workflow interroge les
nouveaux passages à chaque exécution : seules les fenêtres des zones sont lues,
ce qui prend quelques secondes par passage.

**Saison 2026 (du 1er septembre au 2 novembre).** 594 mesures pour 19 zones, dont
267 exploitables ; le reste est perdu sous les nuages, la neige ou l'ombre du
relief. Le nombre de passages utiles varie de 9 à 27 selon la zone.

- **Vars** : les trois zones, mesurées indépendamment, donnent la même courbe
  (24 le 5 octobre, ~50 le 15, 94 à 99 le 22) et le même pic observé, le
  **22 octobre**, suivi de la chute des aiguilles fin octobre.
- **Massifs plus bas ou plus au sud** (Ubaye, Haut-Verdon, Haute-Tinée,
  Vallouise) : coloration plus tardive, pic estimé début novembre.
- **Chamonix-Lognan** : le satellite reste en « début de coloration » (31 au
  plus) alors que la webcam montrait des mélèzes très dorés le 2 novembre. Le
  disque de 4 km mêle plusieurs versants et altitudes : c'est le genre d'écart
  que la comparaison webcam − satellite doit signaler.
- **Les Menuires** : seulement 16 ha de mélèze à moins de 4 km, donc une mesure
  plus bruitée.

L'échelle (0,12) et les filtres ont été réglés sur cette première saison. La
comparaison avec les webcams, affichée sur chaque fiche dès que les deux
courbes ont des jours communs, permettra de les affiner.

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
