# Home Assistant Gaz De Bordeaux integration

## Installation
### Method 1 : HACS (recommended)

Follow the steps described below to add GrDF Gazpar integration with [HACS](https://hacs.xyz/):

1. From [HACS](https://hacs.xyz/) (Home Assistant Community Store), search for "Gaz de Bordeaux".

2. Click to open and click on `DOWNLOAD` button on the lower right of this window.

3. Keep the last version and click the button `DOWNLOAD` on the lower right.

4. The integration is now downloaded but it remains to be configured.

### Method 2 : Manual

Copy the gazdebordeaux directory in HA config/custom_components/gazdebordeaux directory.

`scp -r ./gazdebordeaux hass:/root/config/custom_components`

## Configuration
After the installation, restart your HA application. You could now add the `Gaz De Bordeaux` directly like any other integration. You should be prompted with a form asking for your login and password.
This is the login you should use to check your consumption on the following website: https://life.gazdebordeaux.fr/

## Home Assistant Energy module integration

You probably want to integrate Gaz De Bordeaux data into the Home Assistant Energy module.

![Dashboard](images/energy_module.png)

In Home Assistant energy configuration panel, you can set directly the sensor `gazdebordeaux:energy_consumption` in the gas consumption section, and `sensor.currently_bill_cost_to_date`

## Specific dashboard

If you prefer a specific dashboard to the Energy module, I recommend using the following template
```yaml
  - title: Energy
    path: energy
    icon: mdi:lightning-bolt
    type: sidebar
    badges: []
    cards:
      - chart_type: line
        period: day
        type: statistics-graph
        entities:
          - entity: gazdebordeaux:energy_consumption
            name: Conso
        stat_types:
          - state
        title: Conso kWh
        hide_legend: true
      - type: statistic
        entity: gazdebordeaux:energy_consumption
        period:
          calendar:
            period: month
        stat_type: change
        view_layout:
          position: sidebar
        name: Month consumption
      - type: statistic
        entity: gazdebordeaux:energy_cost
        period:
          calendar:
            period: month
        stat_type: change
        unit: €
        view_layout:
          position: sidebar
        name: Month cost
      - type: entities
        entities:
          - entity: sensor.current_bill_gas_cost_to_date
            icon: mdi:currency-eur
          - entity: sensor.current_bill_gas_usage_to_date
          - entity: sensor.current_energy_usage_to_date
        view_layout:
          position: sidebar
      - chart_type: line
        period: day
        type: statistics-graph
        entities:
          - entity: gazdebordeaux:energy_cost
            name: Coût
        stat_types:
          - state
        hide_legend: true
        title: Coût €
```
