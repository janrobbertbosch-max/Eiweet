import streamlit as st
import pandas as pd
import re
import gspread
import time
import datetime
import requests
from oauth2client.service_account import ServiceAccountCredentials

# --- 1. CONFIGURATIE & HELPER FUNCTIES ---

def get_google_sheet_client():
    """Haalt credentials uit Streamlit Secrets."""
    try:
        creds_dict = st.secrets["gcp_service_account"]
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"Sleutel-fout: Zorg dat gcp_service_account in je secrets staat. Error: {e}")
        return None

def get_gemini_key():
    return st.secrets["GEMINI_API_KEY"]

def call_gemini(prompt, model="gemini-2.0-flash-lite"):
    """Universele helper om de Gemini API aan te roepen."""
    API_KEY = get_gemini_key()
    url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1}
    }
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    return response.json()['candidates'][0]['content']['parts'][0]['text'].strip()

# --- 2. DE 6 HOOFDFUNCTIES (Scripts) ---

def run_prep_ingredients():
    """Stap 1: Opschonen en Masterlijst aanvullen met rapportage"""
    client = get_google_sheet_client()
    
    # Veiligheidscheck: als de verbinding faalt, stop de functie
    if client is None:
        st.error("❌ Kan geen verbinding maken met Google Sheets. Controleer je secrets.toml!")
        return

    with st.status("Stap 1: Ingrediënten voorbereiden...") as status:
        st.write("🔄 Data inladen uit Google Sheets...")
        spreadsheet = client.open("Eiweet validatie met AI")
        sheet_master = spreadsheet.worksheet("Ingredienten Database")
        sheet_products = spreadsheet.worksheet("Producten Input")
        
        df_master = pd.DataFrame(sheet_master.get_all_records())
        df_products = pd.DataFrame(sheet_products.get_all_records())

        difficult_words = ["edelgist", "gistvlokken", "gistextract", "gist", "sheaboter", "shea", "palmvet", "palmolie", "ingredienten:", "ca", "gedroogd", "gepasteuriseerd"]

        def sanitize(ingr):
            if not isinstance(ingr, str) or ingr == "": return ""
            
            # 1. Basis opschoning
            if "Ingrediënten:" in ingr: 
                ingr = ingr.split("Ingrediënten:", 1)[1]
            
            sanitized = re.split(r"\bsporen\b|kan.*bevatten", ingr, flags=re.IGNORECASE)[0]
            
            for word in difficult_words:
                sanitized = re.sub(rf"\b{re.escape(word)}\b", "", sanitized, flags=re.IGNORECASE)
            
            sanitized = re.sub(r"\d+([\.,]\d+)?\s*%", "", sanitized)

            # 2. SLIMME HAAKJES LOGICA
            def smart_brackets(match):
                content = match.group(1) # De tekst tussen de haakjes
                # Als er een komma in de tekst staat, is het een opsomming
                if "," in content:
                    return " " + content + " " # Haakjes weg, inhoud blijft
                else:
                    return f"({content})" # Laat staan (of gebruik match.group(0))

            # Pas dit toe op (), [], <> en {}
            # De regex r"[\(\{\[\<] (.*?) [\)\}\]\>]" zoekt tekst tussen alle soorten haakjes
            sanitized = re.sub(r"[\(\{\[](.*?)[\)\}\]]", smart_brackets, sanitized)

            # 3. Verwijder overige leestekens (behalve de haakjes die we wilden laten staan)
            # We verwijderen nu puntkomma's, punten, etc. maar laten letters/cijfers en haakjes met rust
            sanitized = re.sub(r"[;,:\.]", " ", sanitized)
            
            # 4. Dubbele spaties en afronding
            return re.sub(r"\s{2,}", " ", sanitized).strip()

        st.write("🧹 Ingrediëntenlijsten opschonen...")
        df_products['Ingredients clean'] = df_products['Ingredienten'].apply(sanitize)
        
        # Sla opgeschoonde data op in de Producten sheet
        df_save_products = df_products[df_products['Productnaam'].astype(str).str.strip() != ""].copy()
        sheet_products.clear()
        sheet_products.update(values=[df_save_products.columns.tolist()] + df_save_products.where(pd.notnull(df_save_products), None).values.tolist(), range_name='A1')
        
        # --- Masterlijst aanvullen ---
        st.write("🔍 Zoeken naar nieuwe ingrediënten voor de masterlijst...")
        def get_list(text):
            return [x.strip() for x in text.split(" ") if len(x.strip()) > 2]
        
        df_products['Ingr_List'] = df_products['Ingredients clean'].apply(get_list)
        df_extracted = df_products[['Productnaam', 'Ingr_List']].explode('Ingr_List').dropna()
        
        # Vergelijken met bestaande masterlijst (alles in lowercase voor de check)
        df_master['tmp'] = df_master['Ingredient'].astype(str).str.lower().str.strip()
        df_extracted['tmp'] = df_extracted['Ingr_List'].astype(str).str.lower().str.strip()
        
        new_items = df_extracted[~df_extracted['tmp'].isin(df_master['tmp'])].drop_duplicates('tmp')
        num_new = len(new_items)

        if num_new > 0:
            new_rows = pd.DataFrame({
                'Ingredient': new_items['Ingr_List'],
                'Eiweet rol': "Onbekend",
                'Classificatie datum': datetime.datetime.now().strftime("%d-%m-%Y"),
                'Bron product': new_items['Productnaam']
            })
            df_final_master = pd.concat([df_master, new_rows], ignore_index=True).drop(columns=['tmp'])
            sheet_master.clear()
            sheet_master.update(values=[df_final_master.columns.tolist()] + df_final_master.fillna("").values.tolist(), range_name='A1')
        
        status.update(label=f"✅ Stap 1 Voltooid: {num_new} nieuwe ingrediënten toegevoegd.", state="complete")
    
    # Beknopte update aan de gebruiker
    st.success(f"**Gereed!** In totaal zijn {len(df_products)} producten verwerkt. Er zijn **{num_new}** nieuwe ingrediënten gevonden en toegevoegd aan de masterlijst voor verdere AI-analyse.")

def run_ai_classifier():
    client = get_google_sheet_client()
    if client is None:
        st.error("❌ Geen verbinding met Google Sheets.")
        return

    with st.expander("🔍 Bekijk AI Systeem Prompt (Stap 2)"):
        st.markdown("**Systeem Instructie:**")
        st.code("Bepaal voor elk ingrediënt:\n1. Is het een bron van eiwit? (Wel/Niet)\n2. Wat is de oorsprong? (Plantaardig/Dierlijk/Niet relevant)\n\nAntwoord STRIKT per regel in dit formaat: ID: ROL, TYPE", language="text")

    with st.status("Stap 2: Classificeer Ingredientenlijst met AI") as status:
        st.write("🔄 Masterlijst ophalen...")
        sheet = client.open("Eiweet validatie met AI").worksheet("Ingredienten Database")
        df = pd.DataFrame(sheet.get_all_records())
        
        # Zoek naar rijen waar de classificatie nog leeg is
        mask = (df['Classificatie'].astype(str).str.strip() == "") | (df['Classificatie'].isna())
        to_process = df[mask].copy()
        total_to_do = len(to_process)
        
        if total_to_do == 0:
            status.update(label="✅ Alles is al geclassificeerd!", state="complete")
            return

        batch_size = 30  
        st.write(f"🤖 AI analyseert {total_to_do} ingrediënten in groepen van {batch_size}...")
        
        count_processed = 0
        indices = to_process.index.tolist()

        for i in range(0, total_to_do, batch_size):
            batch_indices = indices[i : i + batch_size]
            batch_num = i // batch_size + 1
            status.write(f"⏳ Verwerken batch {batch_num}...")

            # Prompt opbouwen
            prompt_items = [f"ID:{idx} | Ingr:{df.at[idx, 'Ingredient']}" for idx in batch_indices]
            prompt = f"""
            Bepaal voor elk ingrediënt:
            1. Is het een bron van eiwit? (Antwoord: Wel of Niet)
            2. Wat is de oorsprong? (Antwoord: Plantaardig of Dierlijk of Niet relevant)

            Antwoord STRIKT per regel in dit formaat: ID: ROL, TYPE
            
            Lijst:
            {chr(10).join(prompt_items)}
            """
            
            try:
                raw_response = call_gemini(prompt)
                
                # --- De Onverwoestbare Parser ---
                for line in raw_response.split('\n'):
                    line = line.strip()
                    if not line: continue
                    
                    # Zoek naar alle getallen in de regel (de eerste is de ID)
                    all_numbers = re.findall(r'\d+', line)
                    
                    if all_numbers:
                        idx = int(all_numbers[0])
                        clean_line = line.lower()
                        
                        # Trefwoorden zoeken (ongevoelig voor formatting)
                        rol = ""
                        if "wel" in clean_line: rol = "Wel"
                        elif "niet" in clean_line: rol = "Niet"
                        
                        oorsprong = ""
                        if "plantaardig" in clean_line: oorsprong = "Plantaardig"
                        elif "dierlijk" in clean_line: oorsprong = "Dierlijk"
                        elif "relevant" in clean_line: oorsprong = "Niet relevant"
                        
                        # Match met DataFrame index
                        if idx in df.index and rol and oorsprong:
                            df.at[idx, 'Eiweet rol'] = rol
                            df.at[idx, 'Classificatie'] = oorsprong
                            df.at[idx, 'Classificatie datum'] = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
                            count_processed += 1
                
                status.write(f"✅ Batch {batch_num} verwerkt ({count_processed}/{total_to_do} totaal)")

            except Exception as e:
                st.error(f"⚠️ Fout in batch {batch_num}: {e}")

            # Tussentijds opslaan elke 3 batches
            if batch_num % 3 == 0:
                sheet.clear()
                sheet.update(values=[df.columns.tolist()] + df.fillna("").values.tolist(), range_name='A1')
                status.write(f"💾 Backup opgeslagen om {datetime.datetime.now().strftime('%H:%M:%S')}")

            time.sleep(0.5)
        
        # Finale opslag
        st.write("💾 Definitieve resultaten opslaan...")
        sheet.clear()
        sheet.update(values=[df.columns.tolist()] + df.fillna("").values.tolist(), range_name='A1')
        
        status.update(label=f"✅ Stap 2 Voltooid: {count_processed} items geclassificeerd.", state="complete")
    
    st.success(f"**Gereed!** De masterlijst is bijgewerkt (laatste update: {datetime.datetime.now().strftime('%H:%M')}).")


def run_first_pass_and_review():
    """Stap 3: Snelle Product Analyse (Batch Mode) met tijd-tracking"""
    client = get_google_sheet_client()
    if client is None:
        st.error("❌ Geen verbinding met Google Sheets.")
        return
    
    with st.expander("🔍 Bekijk AI Systeem Prompt (Stap 3 - Product Analyse)"):
        st.markdown("**Systeem Instructie (Expert Persona):**")
        st.code("""
        Je bent een senior voedingsmiddelenexpert gespecialiseerd in eiwitbronnen. 
        Classificeer de volgende producten strikt als 'Plantaardig', 'Dierlijk' of 'Combinatie'.
        Geef per product één korte zin uitleg (rationale).

        Antwoord STRIKT in dit formaat: ID: oordeel | rationale
        """, language="text")
    
    with st.status("Stap 3: Check Eiweetgroep van alle producten met AI...") as status:
        st.write("🔄 Productdata ophalen uit Google Sheets...")
        sheet = client.open("Eiweet validatie met AI").worksheet("Producten Input")
        df = pd.DataFrame(sheet.get_all_records())

        # Filter producten die nog een oordeel nodig hebben
        geldige_class = ['Dierlijk', 'Plantaardig', 'Combinatie']
        mask = (~df['First pass AI'].astype(str).isin(geldige_class))
        to_process = df[mask].copy()
        
        total_to_process = len(to_process)

        if total_to_process > 0:
            batch_size = 20 
            st.write(f"🚀 Batching geactiveerd: {total_to_process} producten in groepen van {batch_size}...")
            
            for i in range(0, total_to_process, batch_size):
                batch_df = to_process.iloc[i : i + batch_size]
                batch_num = i // batch_size + 1
                status.write(f"⏳ Verwerken batch {batch_num}...")

                # Prompt opbouwen met expert-persona en vraag naar rationale
                prompt_items = [f"ID:{idx} | Product:{row['Productnaam']}" for idx, row in batch_df.iterrows()]
                prompt = f"""
                Je bent een senior voedingsmiddelenexpert gespecialiseerd in eiwitbronnen. 
                Classificeer de volgende producten strikt als 'Plantaardig', 'Dierlijk' of 'Combinatie'.
                Geef per product één korte zin uitleg (rationale).

                Antwoord ALLEEN in dit exacte formaat, zonder extra tekst:

                ID:<ID> | oordeel:<Plantaardig/Dierlijk/Combinatie> | rationale:<korte uitleg>

                Voorbeeld:
                ID:123 | oordeel:Plantaardig | rationale:Gemaakt van sojabonen, volledig plantaardig.
                
                Geef voor elke regel in de lijst hieronder precies één regel output in hetzelfde formaat.
                Producten:
                {chr(10).join(prompt_items)}
                """
                
                try:
                    raw_response = call_gemini(prompt)
                    # ANKE toevoeging Debug: sla ruwe AI-output op voor alle rijen in deze batch
                    # for idx in batch_df.index:
                    #     df.at[idx, 'Raw AI response'] = raw_response
                    # ANKE einde toevoeging
                    matches_in_batch = 0
                    
                    # De Onverwoestbare Parser met Rationale-ondersteuning
                    # for line in raw_response.split('\n'):
                    #     line = line.strip()
                    #     if not line or ":" not in line:
                    #         continue
                        
                     # AI-output in losse regels splitsen
                    lines = [l.strip() for l in raw_response.split("\n") if "ID:" in l]


                    for i, line in enumerate(lines):
                        if i >= len(batch_df):
                            break

                        real_idx = batch_df.index[i]
                        # ANKE: sla het AI-antwoord per rij op
                        df.at[real_idx,'AI first pass antwoord'] = line

                        # 2. Oordeel ophalen
                        oordeel_match = re.search(r'oordeel\s*[:：]\s*([A-Za-zÀ-ÿ]+)', line, re.IGNORECASE)
                        if not oordeel_match:
                            st.write("GEEN OORDEEL GEVONDEN IN:", line)
                            continue
                        oordeel = oordeel_match.group(1).capitalize()

                        # 3. Rationale ophalen
                        rationale_match = re.search(r'rationale\s*:\s*(.+)', line, re.IGNORECASE)
                        rationale = rationale_match.group(1).strip() if rationale_match else ""

                        # 4. Opslaan in DataFrame als ID bestaat en oordeel herkend is
                        #if idx in df.index and oordeel:
                        df.at[real_idx, 'First pass AI'] = oordeel
                        df.at[real_idx, 'AI rationale'] = rationale
                        # Timestamp met datum en tijd
                        df.at[real_idx, 'First pass AI datum'] = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
                        matches_in_batch += 1
                    
                    status.write(f"✅ Batch {batch_num} klaar: {matches_in_batch}/{len(batch_df)} producten herkend.")
                    
                except Exception as e:
                    st.error(f"⚠️ Fout in batch {batch_num}: {e}")
                    
                # Tussentijds opslaan om de 3 batches voor maximale veiligheid
                if batch_num % 3 == 0:
                    # Anke vervanging om error te voorkomen:
                    #sheet.update(values=[df.columns.tolist()] + df.where(pd.notnull(df), None).values.tolist(), range_name='A1')
                    sheet.clear()
                    sheet.update("A1", [df.columns.tolist()])
                    sheet.update("A2", df.astype(str).where(pd.notnull(df), "").values.tolist())

                    status.write(f"💾 Tussentijdse backup opgeslagen om {datetime.datetime.now().strftime('%H:%M:%S')}")

                # Korte pauze voor API stabiliteit
                time.sleep(0.5)
        else:
            st.write("✅ Alle producten zijn al voorzien van een 'First pass AI' label.")

        # --- B. STANDAARDISATIE & VERGELIJKING ---
        st.write("📊 Vergelijken met supermarkt labels...")
        
        def standardize(val):
            val = str(val).lower()
            if 'combi' in val: return 'Combinatie'
            if 'plantaardig' in val: return 'Plantaardig'
            if 'dierlijk' in val: return 'Dierlijk'
            return 'Onbekend'

        df['Gestandaardiseerd supermarkt label'] = df['Eiweetgroep Supermarkt'].apply(standardize)
        
        def determine_review(row):
            ai_val = str(row['First pass AI']).strip()
            supermarkt_val = str(row['Gestandaardiseerd supermarkt label']).strip()
            if ai_val == "" or supermarkt_val == "Onbekend":
                return "ja"
            return "nee" if ai_val == supermarkt_val else "ja"

        df['Review nodig'] = df.apply(determine_review, axis=1)
        
        num_reviews = len(df[df['Review nodig'] == "ja"])

        # Finale opslag
        st.write("💾 Definitieve resultaten opslaan...")
        sheet.clear()
        sheet.update(values=[df.columns.tolist()] + df.where(pd.notnull(df), None).values.tolist(), range_name='A1')
        
        status.update(label=f"✅ Stap 3 Voltooid! {num_reviews} reviews gemarkeerd.", state="complete")
    
    # Eindrapportage
    if num_reviews > 0:
        st.warning(f"**Gereed!** Er zijn {num_reviews} producten gevonden waar de AI afwijkt van de supermarkt. Zie kolom 'Review nodig'.")
    else:
        st.success("**Gereed!** De AI is het volledig eens met de supermarkt labels.")
        current_time_str = datetime.datetime.now().strftime("%d-%m-%Y %H:%M:%S")

def run_ingredient_logic():
    """Stap 4: Diepe analyse op basis van de ingrediënten-masterlijst"""
    client = get_google_sheet_client()
    if client is None:
        st.error("❌ Geen verbinding met Google Sheets.")
        return

    with st.status("Stap 4: Ingrediënten-check per product...") as status:
        st.write("🔄 Data ophalen uit beide tabbladen...")
        ss = client.open("Eiweet validatie met AI")
        
        # Haal de masterlijst en de producten op
        df_master = pd.DataFrame(ss.worksheet("Ingredienten Database").get_all_records())
        sheet_p = ss.worksheet("Producten Input")
        df_p = pd.DataFrame(sheet_p.get_all_records())

        # Maak een 'opzoekboek' (dictionary) van de masterlijst voor snelheid
        # Formaat: { 'melk': ('wel', 'dierlijk'), 'water': ('niet', 'n.v.t.') }
        m_dict = {
            str(r['Ingredient']).lower().strip(): (str(r['Eiweet rol']).lower().strip(), str(r['Classificatie']).lower().strip()) 
            for _, r in df_master.iterrows()
        }

        st.write(f"🔬 Analyse van {len(df_p)} producten op ingrediënt-niveau...")

        for idx, row in df_p.iterrows():
            clean_text = str(row['Ingredients clean']).lower()
            ingrs = [x.strip() for x in re.split(r"[ ,]", clean_text) if len(x.strip()) > 2]
            
            found_plant = []
            found_dier = []
            
            for i in ingrs:
                if i in m_dict:
                    rol, cl = m_dict[i]
                    if rol == 'wel':
                        if 'plantaardig' in cl:
                            found_plant.append(i.capitalize())
                        elif 'dierlijk' in cl:
                            found_dier.append(i.capitalize())
            
            found_plant = list(set(found_plant))
            found_dier = list(set(found_dier))
            all_wel = found_plant + found_dier
            
            # --- DE CRUCIALE STAP: Alleen verwerken bij een match ---
            if all_wel:
                if found_plant and found_dier:
                    cat = "Combinatie"
                    ingredientrationale = f"{found_plant[0]} is plantaardig en {found_dier[0]} is dierlijk."
                elif found_plant:
                    cat = "Plantaardig"
                    ingredientrationale = f"Bevat plantaardige bron(nen): {', '.join(found_plant)}."
                elif found_dier:
                    cat = "Dierlijk"
                    ingredientrationale = f"Bevat dierlijke bron(nen): {', '.join(found_dier)}."
                else:
                    cat = "Onbekend"
                    ingredientrationale = "Eiwitbronnen gevonden maar type onbekend."

                # Update alleen deze specifieke velden in het DataFrame
                df_p.at[idx, 'Ingredienten gebaseerde eiweet groep'] = cat
                df_p.at[idx, 'Eiwitbronnen'] = ", ".join(all_wel)
                df_p.at[idx, 'AI ingredientrationale'] = ingredientrationale

        # Update de 'Handmatige review nodig' vlag
        # We vlaggen het product als de supermarkt-label afwijkt van BEIDE AI-checks
        def final_review_check(row):
            sm_label = str(row['Gestandaardiseerd supermarkt label'])
            ai_first = str(row['First pass AI'])
            ingr_label = str(row['Ingredienten gebaseerde eiweet groep'])
            
            if sm_label != ai_first and sm_label != ingr_label:
                return "Ja"
            return "Nee"

        df_p['Handmatige review nodig'] = df_p.apply(final_review_check, axis=1)

        # Tellers voor rapportage
        num_review_final = len(df_p[df_p['Handmatige review nodig'] == "Ja"])
        
        st.write("💾 Resultaten opslaan in Google Sheets...")
        sheet_p.clear()
        sheet_p.update(values=[df_p.columns.tolist()] + df_p.where(pd.notnull(df_p), None).values.tolist(), range_name='A1')
        
        status.update(label=f"✅ Stap 4 Voltooid. {num_review_final} producten vallen buiten de boot.", state="complete")

    # Rapportage
    if num_review_final > 0:
        st.warning(f"**Diepe analyse voltooid.** Voor {num_review_final} producten spreken zowel de AI-schatting als de ingrediënten-check de supermarkt tegen. Deze vereisen echt een handmatige controle.")
    else:
        st.success("**Diepe analyse voltooid.** Alle producten konden succesvol worden onderbouwd door de ingrediëntenlijst.")

def run_reports():
    """Stap 5: Genereer Vendor Rapporten per supermarkt"""
    client = get_google_sheet_client()
    if client is None:
        st.error("❌ Geen verbinding met Google Sheets.")
        return

    with st.status("Stap 5: Rapporten per supermarkt genereren...") as status:
        st.write("🔄 Hoofdtabel inladen...")
        ss = client.open("Eiweet validatie met AI")
        sheet_p = ss.worksheet("Producten Input")
        df = pd.DataFrame(sheet_p.get_all_records())
        
        # Haal alle unieke supermarkten op (en negeer lege cellen)
        vendors = [v for v in df['Supermarkt'].unique() if v and str(v).strip() != ""]
        total_vendors = len(vendors)
        
        if total_vendors == 0:
            status.update(label="⚠️ Geen supermarkten gevonden in de kolom 'Supermarkt'.", state="complete")
            return

        st.write(f"📊 Er worden rapporten gemaakt voor {total_vendors} supermarkten...")

        for i, v in enumerate(vendors):
            status.write(f"Bezig met {i+1}/{total_vendors}: **{v}**")
            
            # Filter de data voor deze specifieke supermarkt
            df_v = df[df['Supermarkt'] == v].copy()
            
            # Maak een geldige naam voor het tabblad (max 31 tekens, geen verboden tekens)
            tab_name = f"Rapport_{str(v).replace(' ', '_')}"[:31]
            
            try:
                # Probeer het tabblad te openen, anders maak het aan
                try:
                    ws = ss.worksheet(tab_name)
                except gspread.exceptions.WorksheetNotFound:
                    ws = ss.add_worksheet(title=tab_name, rows="1000", cols="20")
                
                # Schrijf de gefilterde data naar het tabblad
                ws.clear()
                # We vullen lege waarden (NaN) in met een lege string om fouten te voorkomen
                data_to_save = [df_v.columns.tolist()] + df_v.fillna("").values.tolist()
                ws.update(values=data_to_save, range_name='A1')
                
            except Exception as e:
                st.error(f"Fout bij maken van rapport voor {v}: {e}")
                continue
        
        status.update(label=f"✅ Klaar! {total_vendors} rapporten zijn bijgewerkt.", state="complete")

    # Eindrapportage
    st.success(f"**Alle rapporten zijn gegenereerd!** Je vindt nu voor elke supermarkt ({', '.join(vendors)}) een apart tabblad in je Google Sheet met de specifieke resultaten.")

def run_full_pipeline():
    """Voert Stap 1 t/m 5 automatisch achter elkaar uit"""
    st.header("🚀 Volledige Pijplijn Starten")
    
    # We maken een grote container voor de voortgang
    with st.container(border=True):
        st.subheader("Voortgang van alle stappen")
        
        # Stap 1: Prep Ingredienten
        st.markdown("### 1️⃣ Ingrediëntenmasterlijst voorbereiden")
        run_prep_ingredients()
        
        # Stap 2: AI Masterlijst Classificatie
        st.markdown("---")
        st.markdown("### 2️⃣ Masterlijst classificeren met AI")
        run_ai_classifier()
        
        # Stap 3 & 4: AI First Pass & Review markering
        st.markdown("---")
        st.markdown("### 3️⃣ & 4️⃣ Product Analyse & Review check")
        run_first_pass_and_review()
        
        # Stap 5: Diepe Ingrediënten Logica
        st.markdown("---")
        st.markdown("### 5️⃣ Diepe Ingrediënten-check (Feit-check)")
        run_ingredient_logic()

    st.success("🎉 De volledige pijplijn is succesvol voltooid! Je Google Sheet is nu volledig up-to-date.")


# --- 3. UI LAYOUT ---

def main():
    st.title("Eiweet Validatie Manager - Anke versie 🌱")

    st.info("""
    ### 📖 Instructies
    Deze app werkt met de bijbehorende Google Sheet: **"Eiweet validatie met AI"**.
    
    1. **Data:** Plak je ruwe productdata in het tabblad **"Producten Input"**.
    2. **Workflow:** Doorloop altijd de stappen **1 t/m 5** in deze volgorde.
    """)

    # Stappen 1 t/m 5 onder elkaar, volledige breedte, dezelfde kleur
    if st.button("1️⃣ Prep Ingrediëntenlijst", use_container_width=True): 
        run_prep_ingredients()
        
    if st.button("2️⃣ AI Classificatie Masterlijst", use_container_width=True): 
        run_ai_classifier()
        
    if st.button("3️⃣ AI Product Analyse", use_container_width=True): 
        run_first_pass_and_review()
        
    if st.button("4️⃣ Diepe Ingrediënten-check", use_container_width=True): 
        run_ingredient_logic()
        
    if st.button("5️⃣ Genereer Supermarkt Rapporten", use_container_width=True): 
        run_reports()

    st.divider()

    # Master Run knop eronder, opvallend rood (primary)
    if st.button("🔥 VOER VOLLEDIGE PIJPLIJN UIT (STAP 1-5)", type="primary", use_container_width=True):
        run_full_pipeline()

if __name__ == "__main__":
    main()