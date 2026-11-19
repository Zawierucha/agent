"""Adapter for GSM-labelled nested TAR archives containing expression matrices."""
from __future__ import annotations
import gzip,io,re,tarfile
from pathlib import Path
from typing import Any
import anndata as ad
import numpy as np
import pandas as pd
from scipy import io as scipy_io
from scipy import sparse
from .base import AdapterContext,LoadCandidate
GSM_RE=re.compile(r"^(GSM\d+)_",re.I)

class NestedTarExpressionAdapter:
 name="nested_tar_expression"
 def discover(self,context:AdapterContext)->list[LoadCandidate]:
  candidates=[]
  for outer in sorted(context.supplementary_dir.glob("*_RAW.tar")):
   if not tarfile.is_tarfile(outer): continue
   with tarfile.open(outer,"r:*") as tf:
    for m in tf:
     if not m.isfile() or not m.name.lower().endswith((".tar.gz",".tgz")): continue
     match=GSM_RE.match(Path(m.name).name)
     if not match: continue
     gsm=match.group(1).upper()
     if gsm not in context.include_gsms: continue
     raw=tf.extractfile(m)
     if raw is None: continue
     blob=raw.read(); raw.close()
     try:
      with tarfile.open(fileobj=io.BytesIO(blob),mode="r:gz") as nt:
       names=[x.name for x in nt.getmembers() if x.isfile() and "/._" not in x.name and not Path(x.name).name.startswith("._")]
     except (tarfile.TarError,EOFError,OSError): continue
     lower={Path(n).name.lower():n for n in names}
     if {"matrix.mtx.gz","barcodes.tsv.gz","features.tsv.gz"}.issubset(lower):
      kind="nested_10x_mtx"; components={"outer_member":m.name,"matrix":lower["matrix.mtx.gz"],"barcodes":lower["barcodes.tsv.gz"],"features":lower["features.tsv.gz"]}
     elif {"matrix.tsv","barcode.tsv","genes.tsv"}.issubset(lower):
      kind="nested_dense_gene_by_cell"; components={"outer_member":m.name,"matrix":lower["matrix.tsv"],"barcodes":lower["barcode.tsv"],"features":lower["genes.tsv"]}
     else: continue
     candidates.append(LoadCandidate(gsm_id=gsm,source_path=outer,source_relative_path=outer.name,format_guess=kind,components=components,archive_member_map=True,warnings=["Nested archive decoded in memory without extracting source files."]))
  return candidates

 @staticmethod
 def _inner_blob(c:LoadCandidate)->bytes:
  with tarfile.open(c.source_path,"r:*") as tf:
   m=tf.getmember(c.components["outer_member"]); f=tf.extractfile(m)
   if f is None: raise ValueError(f"Cannot open nested archive {m.name}")
   return f.read()
 @staticmethod
 def _member(blob:bytes,name:str)->bytes:
  with tarfile.open(fileobj=io.BytesIO(blob),mode="r:gz") as tf:
   m=tf.getmember(name); f=tf.extractfile(m)
   if f is None: raise ValueError(f"Cannot open nested member {name}")
   return f.read()
 @staticmethod
 def _lines(data:bytes,gz:bool=False)->list[str]:
  if gz: data=gzip.decompress(data)
  return [x for x in data.decode("utf-8-sig").splitlines() if x.strip()]
 @staticmethod
 def _frame(n:int,values:dict[str,Any],specs:dict[str,Any])->pd.DataFrame:
  f=pd.DataFrame(index=range(n))
  for col,spec in specs.items():
   if col=="index": continue
   f[col]=pd.Series(values.get(col,[pd.NA]*n),dtype=spec["dtype"])
  return f
 def _common(self,context:AdapterContext,c:LoadCandidate,x:sparse.csr_matrix,barcodes:list[str],feature_ids:list[str],feature_names:list[str|None],feature_types:list[str|None],semantics:str,fmt:str)->ad.AnnData:
  if x.shape!=(len(barcodes),len(feature_ids)): raise ValueError(f"Matrix shape {x.shape} does not match cells/features {(len(barcodes),len(feature_ids))}")
  if len(set(barcodes))!=len(barcodes): raise ValueError("Barcodes are duplicated")
  if len(set(feature_ids))!=len(feature_ids): raise ValueError("Feature IDs are duplicated; refusing to alter source identifiers")
  if not np.isfinite(x.data).all() or (x.data<0).any(): raise ValueError("Matrix contains negative or non-finite values")
  x=x.tocsr().astype(np.float32); n=len(barcodes); sample=context.sample_metadata.get(c.gsm_id,{}); contract=context.canonical_schema["anndata_contract"]
  is_counts=semantics=="raw counts"
  vals={"sample_id":[c.gsm_id]*n,"source_barcode":barcodes,"donor_id":[sample.get("patient_id") or pd.NA]*n,"organism":[sample.get("organism") or pd.NA]*n,"tissue":[sample.get("tissue") or sample.get("tissue_source") or pd.NA]*n,"cell_type":[pd.NA]*n,"disease":[sample.get("disease") or sample.get("disease_status") or pd.NA]*n,"condition":[sample.get("disease_status") or pd.NA]*n,"assay":[pd.NA]*n,"modality":[context.manifest.get("stored_dataset_qualification",{}).get("modality") or pd.NA]*n,"timepoint":[sample.get("timepoint") or sample.get("timepoint_value") or pd.NA]*n,"source_accession":[context.gse_id]*n,"n_counts":np.asarray(x.sum(axis=1)).ravel().astype(np.int64).tolist() if is_counts else [pd.NA]*n,"n_features":np.diff(x.indptr).astype(np.int32).tolist(),"percent_mito":[pd.NA]*n}
  obs=self._frame(n,vals,contract["obs"]); obs.index=pd.Index([f"{c.gsm_id}:{b}" for b in barcodes],name="cell_id")
  var=self._frame(len(feature_ids),{"feature_name":feature_names,"feature_type":feature_types,"genome_build":[pd.NA]*len(feature_ids)},contract["var"]); var.index=pd.Index(feature_ids,name="feature_id")
  a=ad.AnnData(X=x,obs=obs,var=var)
  if is_counts: a.layers["counts"]=x.copy(); a.uns["counts_confirmed"]=True
  a.uns.update({"schema_version":context.canonical_schema["schema_version"],"schema_id":context.canonical_schema["schema_id"],"schema_sha256":context.schema_sha256,"source_gse":context.gse_id,"matrix_semantics":semantics,"feature_id_namespace":"source-supplied feature labels","harmonization_notes":"Loaded without normalization from a GSM-labelled nested archive; orientation and identifiers were validated against source components.","source_format":{"format":fmt,"file":c.source_relative_path,"outer_member":c.components["outer_member"],"components":c.components}})
  src=next((i for i in context.manifest.get("files",[]) if i.get("filename")==c.source_path.name),None)
  if src and src.get("sha256"): a.uns["source_format"]["source_sha256"]=src["sha256"]
  return a
 def load(self,context:AdapterContext,c:LoadCandidate)->ad.AnnData:
  blob=self._inner_blob(c)
  if c.format_guess=="nested_10x_mtx":
   matrix_raw=gzip.decompress(self._member(blob,c.components["matrix"])); mat=scipy_io.mmread(io.BytesIO(matrix_raw))
   if not sparse.issparse(mat): mat=sparse.coo_matrix(mat)
   mat=mat.tocsr()
   bars=self._lines(self._member(blob,c.components["barcodes"]),True); flines=self._lines(self._member(blob,c.components["features"]),True)
   ids=[]; names=[]; types=[]
   for line in flines:
    p=line.split("\t"); ids.append(p[0].strip()); names.append(p[1].strip() if len(p)>1 and p[1].strip() else p[0].strip()); types.append(p[2].strip() if len(p)>2 and p[2].strip() else None)
   if mat.shape!=(len(ids),len(bars)): raise ValueError(f"MatrixMarket shape {mat.shape} disagrees with features/barcodes {(len(ids),len(bars))}")
   if not np.equal(mat.data,np.floor(mat.data)).all(): raise ValueError("Count-path MatrixMarket contains non-integer values")
   return self._common(context,c,mat.transpose().tocsr(),bars,ids,names,types,"raw counts","GSM-labelled nested 10x-like Matrix Market count triplet")
  genes=self._lines(self._member(blob,c.components["features"])); bars_lines=self._lines(self._member(blob,c.components["barcodes"])); matrix=self._lines(self._member(blob,c.components["matrix"]))
  if not genes or genes[0].split("\t")[:2]!=["Gene.Name","Ensembl_ID"]: raise ValueError("Unexpected genes.tsv header")
  supplied_gene_names={x.split("\t",1)[0].strip() for x in genes[1:] if x.strip()}
  if not bars_lines or bars_lines[0].strip().lower()!="barcode": raise ValueError("Unexpected barcode.tsv header")
  bars=[x.strip() for x in bars_lines[1:]]; header=matrix[0].split("\t")
  if header[0]!="Gene.Name" or header[1:]!=bars: raise ValueError("Dense matrix header does not exactly match barcode.tsv")
  rr=[];cc=[];dd=[];ids=[]
  for i,line in enumerate(matrix[1:]):
   p=line.split("\t"); feature=p[0].strip()
   if not feature or feature not in supplied_gene_names: raise ValueError(f"Dense matrix feature {feature!r} is absent from genes.tsv")
   ids.append(feature)
   v=np.asarray([float(x) for x in p[1:]],dtype=np.float64)
   if len(v)!=len(bars): raise ValueError(f"Dense matrix row {i+2} has wrong width")
   nz=np.flatnonzero(v)
   if len(nz): rr.extend([i]*len(nz)); cc.extend(nz.tolist()); dd.extend(v[nz].astype(np.float32).tolist())
  if not ids or len(set(ids))!=len(ids): raise ValueError("Dense matrix feature labels are empty or duplicated")
  fbc=sparse.coo_matrix((dd,(rr,cc)),shape=(len(ids),len(bars)),dtype=np.float32)
  return self._common(context,c,fbc.transpose().tocsr(),bars,ids,ids.copy(),[None]*len(ids),"unknown","GSM-labelled nested dense gene-by-cell TSV")
ADAPTER=NestedTarExpressionAdapter()
