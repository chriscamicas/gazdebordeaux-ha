# Home Assistant Gaz De Bordeaux integration
Since Gaz De Bordeaux is a France specific provider, this README is in French

## Installation
### Method 1 : HACS (recommended)

Follow the steps described below to add GrDF Gazpar integration with [HACS](https://hacs.xyz/):

1. From [HACS](https://hacs.xyz/) (Home Assistant Community Store), open the upper left menu and select `Custom repositories` option to add the new repo.

2. Add the address <https://github.com/chriscamicas/gazdebordeaux-ha> with the category `Integration`, and click `ADD`. The new corresponding repo appears in the repo list.

3. Select this repo (this integration description is displayed in a window) and click on `INSTALL THIS REPOSITORY` button on the lower right of this window.

4. Keep the last version and click the button `INSTALL` on the lower right.

5. Do click on `RELOAD` button for completion! The integration is now ready. It remains the configuration.

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