import streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai
from PIL import Image
import os

# Configuración de la página Streamlit (debe ser lo primero)
st.set_page_config(page_title="Simulador de Bioetanol Didáctico", layout="wide")

# =================================================================
# 1. ENCAPSULAMIENTO DE LA LÓGICA DE BIOSTEAM
# =================================================================
# Esta función crea y simula el proceso desde cero cada vez que se llama.
def correr_simulacion(flow_water, flow_eth, temp_mosto, pres_mosto, T_flash, P_flash, 
                      precio_elec, precio_vapor, precio_agua, precio_mp):
    
    # IMPORTANTE: Limpiar el flowsheet anterior para evitar errores de ID duplicado
    bst.main_flowsheet.clear()
    
    # Definimos los compuestos químicos (localmente)
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)

    # --- Configuración de Precios Económicos ---
    bst.PowerUtility.price = precio_elec # $/kWh
    
    vapor = bst.HeatUtility.get_agent("low_pressure_steam")
    vapor.heat_transfer_price = precio_vapor # $/MJ
    
    agua = bst.HeatUtility.get_agent("cooling_water")
    agua.heat_transfer_price = precio_agua # $/MJ

    # =================================================================
    # 2. DEFINICIÓN DE CORRIENTES Y EQUIPOS
    # =================================================================
    
    # Alimentación (usando parámetros dinámicos)
    mosto = bst.Stream("1_MOSTO",
                       Water=flow_water, Ethanol=flow_eth, units="kg/hr",
                       T=temp_mosto + 273.15, # C to K
                       P=pres_mosto * 101325) # atm to Pa
    mosto.price = precio_mp # $/kg

    # Corriente de reciclo vacía inicialmente
    vinazas_retorno = bst.Stream("Vinazas_Retorno", T=95+273.15, P=3*101325)

    # Equipos
    P100 = bst.Pump("P100", ins=mosto, P=4*101325)
    
    W210 = bst.HXprocess("W210", ins=(P100-0, vinazas_retorno), 
                        outs=("3_Mosto_Pre", "Drenaje"),
                        phase0="l", phase1="l")
    W210.outs[0].T = 85 + 273.15

    W220 = bst.HXutility("W220", ins=W210-0, outs="Mezcla", T=T_flash+273.15)
    
    V100 = bst.IsenthalpicValve("V100", ins=W220-0, outs="Mezcla_Bifasica", P=P_flash*101325)

    # Flash (parámetros dinámicos)
    V1 = bst.Flash("V1", ins=V100-0, outs=("Vapor_caliente", "Vinazas"), P=P_flash*101325, Q=0)

    # Corrección de acceso a energía: El condensador usa utility
    W310 = bst.HXutility("W310", ins=V1-0, outs="Producto_Final", T=25+273.15)
    producto = W310.outs[0]
    producto.price = 1.2 # $/kg fijo para este ejemplo

    P200 = bst.Pump("P200", ins=V1-1, outs=vinazas_retorno, P=3*101325)

    # =================================================================
    # 3. SIMULACIÓN
    # =================================================================
    eth_sys = bst.System("planta_etanol", path=(P100, W210, W220, V100, V1, W310, P200))
    
    sim_exitosa = True
    try:
        eth_sys.simulate()
    except Exception as e:
        sim_exitosa = False
        return None, None, None, None, f"Error de convergencia: {e}"

    # =================================================================
    # 4. GENERACIÓN DE REPORTES (Lógica original adaptada)
    # =================================================================
    # --- Materia ---
    datos_mat = []
    for s in eth_sys.streams:
        if s.F_mass > 0.01: # Filtro robusto
            datos_mat.append({
                "Corriente": s.ID,
                "Temp (°C)": round(s.T - 273.15, 2),
                "Presión (bar)": round(s.P / 1e5, 2),
                "Flujo (kg/h)": round(s.F_mass, 2),
                "% Etanol": f"{s.imass['Ethanol'] / s.F_mass:.1%}" if s.F_mass > 0 else "0%",
                "% Agua": f"{s.imass['Water'] / s.F_mass:.1%}" if s.F_mass > 0 else "0%"
            })
    df_mat = pd.DataFrame(datos_mat)

    # --- Energía (Corrección Específica) ---
    datos_en = []
    for u in eth_sys.units:
        calor_kw = 0.0
        tipo = "-"
        
        # HXprocess: Recuperación interna
        if isinstance(u, bst.HXprocess):
            calor_kw = (u.outs[0].H - u.ins[0].H) / 3600
            tipo = "Recuperación Interna"
        
        # HXutility: Servicios auxiliares (W220, W310)
        elif isinstance(u, bst.HXutility):
            # Acceso correcto al duty de la utilidad
            calor_kw = u.duty / 3600
            if calor_kw > 0: tipo = "Calentamiento (Vapor)"
            elif calor_kw < 0: tipo = "Enfriamiento (Agua)"

        # Bombas y equipos con consumo eléctrico
        potencia = u.power_utility.rate if u.power_utility else 0.0

        if abs(calor_kw) > 0.1:
            datos_en.append({"Equipo": u.ID, "Tipo": tipo, "Energía Térmica (kW)": round(calor_kw, 2)})
        if potencia > 0.1:
            datos_en.append({"Equipo": u.ID, "Tipo": "Electricidad", "Potencia (kW)": round(potencia, 2)})
            
    df_en = pd.DataFrame(datos_en)

    # --- Economía (TEA Didáctico original adaptado) ---
    class TEA_Didactico(bst.TEA):
        def _DPI(self, installed_equipment_cost): return self.purchase_cost
        def _TDC(self, DPI): return DPI
        def _FCI(self, TDC): return self.purchase_cost * self.lang_factor
        def _TCI(self, FCI): return FCI + self.WC
        def _FOC(self, FCI): return 0.0
        @property
        def VOC(self):
            mat = getattr(self.system, 'material_cost', 0)
            util = getattr(self.system, 'utility_cost', 0)
            return mat + util

    tea = TEA_Didactico(system=eth_sys, IRR=0.15, duration=(2025, 2045), income_tax=0.3,
                        depreciation="MACRS7", construction_schedule=(0.4, 0.6), operating_days=330,
                        lang_factor=4.0, WC_over_FCI=0.05)
    
    # Cálculos económicos
    tea.IRR = 0.0
    costo_prod = tea.solve_price(producto)
    tea.IRR = 0.15
    precio_venta = tea.solve_price(producto)
    
    ind_econ = {
        "Costo Producción ($/kg)": costo_prod,
        "Precio Venta Meta ($/kg)": precio_venta,
        "NPV (MUSD)": tea.NPV / 1e6,
        "PBP (Años)": tea.PBP,
        "ROI (%)": tea.ROI * 100
    }

    # --- Diagrama (PFD) ---
    # Solución para PFD en Web: Generar un archivo temporal único
    import uuid
    diag_id = str(uuid.uuid4())[:8]
    pfd_filename = f"pfd_{diag_id}"
    try:
        eth_sys.diagram(file=pfd_filename, format="png", display=False)
        pfd_path = pfd_filename + ".png"
    except:
        pfd_path = None

    return df_mat, df_en, ind_econ, pfd_path, None

# =================================================================
# 5. INTEGRACIÓN DE IA (GEMINI)
# =================================================================
def consultar_tutor_ia(df_materia, ind_econ, api_key):
    if not api_key:
        return "⚠️ Por favor configura la GEMINI_API_KEY en los Secrets de Streamlit."
    
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-pro')
    
    # Convertir datos relevantes a texto para el prompt
    prod_final = df_materia[df_materia['Corriente'] == 'Producto_Final'].to_string()
    
    prompt = f"""
    Actúa como un profesor experto en Ingeniería Química. Analiza los siguientes resultados de una simulación BioSTEAM de una separación Flash Agua-Etanol.
    
    Datos del Producto Final:
    {prod_final}
    
    Indicadores Económicos:
    {ind_econ}
    
    Por favor, proporciona una interpretación educativa de estos resultados para un estudiante.
    1. Explica si la separación fue efectiva basándote en la pureza del etanol.
    2. Analiza la viabilidad económica (NPV, ROI) y qué factor (materia prima, utilidades) influye más.
    3. Sugiere una mejora técnica al proceso (ej. usar destilación en lugar de solo flash).
    Mantén un tono didáctico y claro.
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Error al contactar a Gemini: {e}"

# =================================================================
# 6. INTERFAZ DE USUARIO (STREAMLIT)
# =================================================================
st.title("🔬 Simulador Interactivo: Separación Flash de Bioetanol")
st.markdown("---")

# --- Barra Lateral (Inputs) ---
st.sidebar.header("⚙️ Parámetros de Proceso")
f_agua = st.sidebar.slider("Flujo Agua Alim. (kg/h)", 500, 2000, 900)
f_eth = st.sidebar.slider("Flujo Etanol Alim. (kg/h)", 50, 300, 100)
t_mosto = st.sidebar.number_input("Temp. Alimentación (°C)", value=25)
p_mosto = st.sidebar.number_input("Presión Alimentación (atm)", value=1.0)

st.sidebar.markdown("---")
st.sidebar.header("🔥 Condiciones Flash V-1")
t_flash = st.sidebar.slider("Temperatura Flash (°C)", 80, 110, 92)
p_flash = st.sidebar.slider("Presión Flash (atm)", 0.5, 2.0, 1.0)

st.sidebar.markdown("---")
st.sidebar.header("💰 Parámetros Económicos")
p_elec = st.sidebar.number_input("Precio Electricidad ($/kWh)", value=0.085, format="%.4f")
p_vapor = st.sidebar.number_input("Precio Vapor ($/MJ)", value=0.025, format="%.4f")
p_agua_c = st.sidebar.number_input("Precio Agua Enfr. ($/MJ)", value=0.0005, format="%.5f")
p_mp = st.sidebar.number_input("Precio Materia Prima ($/kg)", value=0.05, format="%.3f")

# Botón principal
run_sim = st.sidebar.button("Correr Simulación", type="primary")

# --- Cuerpo Principal ---
if run_sim:
    with st.spinner('Ejecutando balance de materia y energía en BioSTEAM...'):
        df_mat, df_en, econ, pfd_file, error = correr_simulacion(
            f_agua, f_eth, t_mosto, p_mosto, t_flash, p_flash,
            p_elec, p_vapor, p_agua_c, p_mp
        )
    
    if error:
        st.error(error)
    else:
        st.success("✅ Simulación finalizada exitosamente.")
        
        # PFD
        st.header("🖼️ Diagrama de Flujo del Proceso (PFD)")
        if pfd_file and os.path.exists(pfd_file):
            image = Image.open(pfd_file)
            st.image(image, caption="PFD generado por BioSTEAM", use_column_width=True)
            os.remove(pfd_file) # Limpieza de archivo temporal
        else:
            st.warning("No se pudo generar el diagrama.")

        # Resultados
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("📊 Balance de Materia")
            st.dataframe(df_mat, use_container_width=True)
            
            st.subheader("📈 Indicadores Económicos (TEA)")
            # Convertir dict a DataFrame para mejor visualización
            df_econ = pd.DataFrame(list(econ.items()), columns=['Indicador', 'Valor'])
            st.table(df_econ)

        with col2:
            st.subheader("⚡ Consumo de Energía")
            st.dataframe(df_en, use_container_width=True)
            
            # Sección de IA
            st.markdown("---")
            st.subheader("🤖 Tutor de Ingeniería (Gemini AI)")
            
            # Obtener API Key de los Secrets
            api_key = st.secrets.get("GEMINI_API_KEY")
            
            if st.button("Pedir interpretación a la IA"):
                with st.spinner('Consultando al tutor experto...'):
                    respuesta_ia = consultar_tutor_ia(df_mat, econ, api_key)
                    st.markdown(respuesta_ia)

else:
    st.info("👈 Ajusta los parámetros en la barra lateral y haz clic en 'Correr Simulación' para ver los resultados.")
    # Imagen de bienvenida o descripción general
    st.markdown("""
    Esta aplicación permite simular un proceso de separación flash de una mezcla Agua-Etanol utilizando la librería científica BioSTEAM.
    Puedes modificar las condiciones de operación y los precios de mercado para ver cómo afectan la pureza del producto y la rentabilidad del proyecto en tiempo real.
    """)
