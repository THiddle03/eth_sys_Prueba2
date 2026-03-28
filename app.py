import streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai
from PIL import Image
import os
import uuid

# =================================================================
# 1. CONFIGURACIÓN DE LA PÁGINA
# =================================================================
st.set_page_config(page_title="BioSTEAM Web Simulator", layout="wide")

# =================================================================
# 2. FUNCIÓN MAESTRA DE SIMULACIÓN
# =================================================================
def correr_simulacion(flow_water, flow_eth, temp_mosto, pres_mosto, T_flash, P_flash, 
                      precio_elec, precio_vapor, precio_agua, precio_mp):
    
    # Limpiar el flowsheet para evitar duplicados
    bst.main_flowsheet.clear()
    
    # Configuración de compuestos
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)

    # Precios de Utilities
    bst.PowerUtility.price = precio_elec
    
    # Configuración de agentes térmicos
    vapor = bst.HeatUtility.get_agent("low_pressure_steam")
    vapor.heat_transfer_price = precio_vapor
    
    agua = bst.HeatUtility.get_agent("cooling_water")
    agua.heat_transfer_price = precio_agua

    # --- CORRIENTES ---
    mosto = bst.Stream("1_MOSTO",
                       Water=flow_water, Ethanol=flow_eth, units="kg/hr",
                       T=temp_mosto + 273.15, 
                       P=pres_mosto * 101325)
    mosto.price = precio_mp

    vinazas_retorno = bst.Stream("Vinazas_Retorno", T=95+273.15, P=3*101325)

    # --- EQUIPOS ---
    P100 = bst.Pump("P100", ins=mosto, P=4*101325)
    
    W210 = bst.HXprocess("W210", ins=(P100-0, vinazas_retorno), 
                        outs=("3_Mosto_Pre", "Drenaje"),
                        phase0="l", phase1="l")
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
        return None, None, None, None, f"Error en simulación: {e}"

    # --- REPORTES ---
    datos_mat = []
    for s in eth_sys.streams:
        if s.F_mass > 0.001:
            datos_mat.append({
                "Corriente": s.ID,
                "Temp (°C)": round(s.T - 273.15, 2),
                "Flujo (kg/h)": round(s.F_mass, 2),
                "% Etanol": f"{(s.imass['Ethanol']/s.F_mass if s.F_mass >0 else 0):.1%}"
            })
    df_mat = pd.DataFrame(datos_mat)

    datos_en = []
    for u in eth_sys.units:
        calor_kw = 0.0
        if isinstance(u, bst.HXprocess):
            calor_kw = (u.outs[0].H - u.ins[0].H) / 3600
        elif hasattr(u, "heat_utilities") and u.heat_utilities:
            calor_kw = sum([hu.duty for hu in u.heat_utilities]) / 3600
        
        potencia = u.power_utility.rate if u.power_utility else 0.0
        if abs(calor_kw) > 0.01 or potencia > 0.01:
            datos_en.append({"Equipo": u.ID, "Calor (kW)": round(calor_kw, 2), "Potencia (kW)": round(potencia, 2)})
    df_en = pd.DataFrame(datos_en)

    # --- TEA CORREGIDO (Compatibilidad Total) ---
    # Usamos una estructura más simple para evitar el TypeError de argumentos
    class TEA_Simple(bst.TEA):
        def __init__(self, system, IRR, duration, income_tax, lang_factor, WC_over_FCI):
            # Llamamos al constructor padre con los argumentos mínimos necesarios
            super().__init__(system, IRR, duration, income_tax, lang_factor, WC_over_FCI)
        
        def _DPI(self, installed_equipment_cost): return self.purchase_cost
        def _TDC(self, DPI): return DPI
        def _FCI(self, TDC): return self.purchase_cost * self.lang_factor
        def _TCI(self, FCI): return FCI + self.WC
        def _FOC(self, FCI): return 0.0
        @property
        def VOC(self):
            return self.system.material_cost + self.system.utility_cost

    # Instanciación con argumentos posicionales claros para evitar el TypeError
    tea = TEA_Simple(
        system=eth_sys, 
        IRR=0.15, 
        duration=(2025, 2045), 
        income_tax=0.3, 
        lang_factor=4.0, 
        WC_over_FCI=0.05
    )
    
    tea.IRR = 0.0
    costo_prod = tea.solve_price(producto)
    tea.IRR = 0.15
    precio_venta = tea.solve_price(producto)
    
    ind_econ = {
        "Costo Producción ($/kg)": round(costo_prod, 3),
        "Precio Venta Meta ($/kg)": round(precio_venta, 3),
        "NPV (MUSD)": round(tea.NPV / 1e6, 2),
        "ROI (%)": round(tea.ROI * 100, 1)
    }

    # --- PFD ---
    pfd_filename = f"pfd_{uuid.uuid4().hex[:8]}"
    try:
        eth_sys.diagram(file=pfd_filename, format="png", display=False)
        pfd_path = pfd_filename + ".png"
    except:
        pfd_path = None

    return df_mat, df_en, ind_econ, pfd_path, None

# =================================================================
# 3. INTERFAZ
# =================================================================
st.sidebar.header("Parámetros")
f_w = st.sidebar.number_input("Agua (kg/h)", value=900)
f_e = st.sidebar.number_input("Etanol (kg/h)", value=100)
t_f = st.sidebar.slider("Temp Flash (°C)", 80, 110, 92)
p_v = st.sidebar.number_input("Vapor ($/MJ)", value=0.025, format="%.4f")
p_mp = st.sidebar.number_input("Materia Prima ($/kg)", value=0.05, format="%.3f")

if st.sidebar.button("Ejecutar"):
    df_m, df_e, econ, pfd, err = correr_simulacion(f_w, f_e, 25, 1, t_f, 1.0, 0.085, p_v, 0.0005, p_mp)
    
    if err:
        st.error(err)
    else:
        if pfd and os.path.exists(pfd):
            st.image(pfd)
            os.remove(pfd)
        
        c1, c2 = st.columns(2)
        with c1:
            st.write("### Materia", df_m)
            st.write("### Economía")
            st.table(pd.DataFrame(list(econ.items())))
        with c2:
            st.write("### Energía", df_e)
            # Gemini
            key = st.secrets.get("GEMINI_API_KEY")
            if key:
                genai.configure(api_key=key)
                m = genai.GenerativeModel('gemini-pro')
                if st.button("Consultar IA"):
                    r = m.generate_content(f"Explica estos resultados químicos: {econ}")
                    st.info(r.text)
