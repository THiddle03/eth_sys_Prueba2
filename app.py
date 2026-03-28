import streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai
from PIL import Image
import os
import uuid

# 1. CONFIGURACIÓN DE PÁGINA
st.set_page_config(page_title="BioSTEAM Web Simulator", layout="wide")

# 2. FUNCIÓN DE SIMULACIÓN
def correr_simulacion(flow_water, flow_eth, temp_mosto, pres_mosto, T_flash, P_flash, 
                      precio_elec, precio_vapor, precio_agua, precio_mp):
    
    bst.main_flowsheet.clear()
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)

    # Configuración de precios
    bst.PowerUtility.price = precio_elec
    vapor = bst.HeatUtility.get_agent("low_pressure_steam")
    vapor.heat_transfer_price = precio_vapor
    agua = bst.HeatUtility.get_agent("cooling_water")
    agua.heat_transfer_price = precio_agua

    # --- CORRIENTES ---
    mosto = bst.Stream("1_MOSTO", Water=flow_water, Ethanol=flow_eth, units="kg/hr",
                       T=temp_mosto + 273.15, P=pres_mosto * 101325)
    mosto.price = precio_mp
    vinazas_retorno = bst.Stream("Vinazas_Retorno", T=95+273.15, P=3*101325)

    # --- EQUIPOS ---
    P100 = bst.Pump("P100", ins=mosto, P=4*101325)
    W210 = bst.HXprocess("W210", ins=(P100-0, vinazas_retorno), outs=("3_Mosto_Pre", "Drenaje"), phase0="l", phase1="l")
    W210.outs[0].T = 85 + 273.15
    W220 = bst.HXutility("W220", ins=W210-0, outs="Mezcla", T=T_flash+273.15)
    V100 = bst.IsenthalpicValve("V100", ins=W220-0, outs="Mezcla_Bifasica", P=P_flash*101325)
    V1 = bst.Flash("V1", ins=V100-0, outs=("Vapor_caliente", "Vinazas"), P=P_flash*101325, Q=0)
    W310 = bst.HXutility("W310", ins=V1-0, outs="Producto_Final", T=25+273.15)
    producto = W310.outs[0]
    producto.price = 1.2
    P200 = bst.Pump("P200", ins=V1-1, outs=vinazas_retorno, P=3*101325)

    # --- SISTEMA ---
    eth_sys = bst.System("planta_etanol", path=(P100, W210, W220, V100, V1, W310, P200))
    
    try:
        eth_sys.simulate()
    except Exception as e:
        return None, None, None, None, f"Error: {e}"

    # --- REPORTES ---
    datos_mat = [{"Corriente": s.ID, "Temp (°C)": round(s.T-273.15, 2), "Flujo (kg/h)": round(s.F_mass, 2), 
                  "% Etanol": f"{(s.imass['Ethanol']/s.F_mass if s.F_mass>0 else 0):.1%}"} 
                 for s in eth_sys.streams if s.F_mass > 0.01]
    df_mat = pd.DataFrame(datos_mat)

    datos_en = []
    for u in eth_sys.units:
        calor = sum([hu.duty for hu in u.heat_utilities])/3600 if hasattr(u, "heat_utilities") else 0
        potencia = u.power_utility.rate if u.power_utility else 0
        if abs(calor) > 0.1 or potencia > 0.1:
            datos_en.append({"Equipo": u.ID, "Calor (kW)": round(calor, 2), "Potencia (kW)": round(potencia, 2)})
    df_en = pd.DataFrame(datos_en)

    # --- TEA ROBUSTO (CORRECCIÓN FINAL) ---
    class TEA_Robusto(bst.TEA):
        def _DPI(self, installed_equipment_cost): return self.purchase_cost
        def _TDC(self, DPI): return DPI
        def _FCI(self, TDC): return self.purchase_cost * self.lang_factor
        def _TCI(self, FCI): return FCI + self.WC
        def _FOC(self, FCI): return 0.0
        @property
        def VOC(self): return self.system.material_cost + self.system.utility_cost

    # Pasamos TODOS los argumentos requeridos por el error
    tea = TEA_Robusto(
        system=eth_sys,
        IRR=0.15,
        duration=(2025, 2045),
        depreciation='MACRS7',
        income_tax=0.3,
        operating_days=330,
        lang_factor=4.0,
        construction_schedule=(0.4, 0.6),
        WC_over_FCI=0.05,
        startup_months=6,
        startup_FOCfrac=0.5,
        startup_VOCfrac=0.5,
        startup_salesfrac=0.5,
        finance_interest=0,
        finance_years=0,
        finance_fraction=0
    )
    
    tea.IRR = 0.0
    costo_p = tea.solve_price(producto)
    tea.IRR = 0.15
    precio_v = tea.solve_price(producto)
    
    ind_econ = {"Costo Producción ($/kg)": round(costo_p, 3), "Precio Venta Meta ($/kg)": round(precio_v, 3),
                "NPV (MUSD)": round(tea.NPV/1e6, 2), "ROI (%)": round(tea
