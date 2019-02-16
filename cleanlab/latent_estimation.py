#!/usr/bin/env python
# coding: utf-8

# ## Latent Estimation
# 
# #### Contains methods for estimating four latent structures used for confident learning.
# * The latent prior of the unobserved, errorless labels $y$: denoted $p(y)$ (latex) & '```py```' (code).
# * The latent noisy channel (noise matrix) characterizing the flipping rates: denoted $P_{s \vert y }$ (latex) & '```nm```' (code).
# * The latent inverse noise matrix characterizing flipping process: denoted $P_{y \vert s}$ (latex) & '```inv```' (code).
# * The latent ```confident_joint```, an unnormalized counts matrix of counting a confident subset of the joint counts of label errors.

# In[ ]:


from __future__ import print_function, absolute_import, division, unicode_literals, with_statement
from sklearn.linear_model import LogisticRegression as logreg
from sklearn.model_selection import StratifiedKFold    
from sklearn.preprocessing import LabelBinarizer 
import numpy as np
import copy

from cleanlab.util import value_counts, clip_values, clip_noise_rates
from cleanlab.latent_algebra import compute_inv_noise_matrix, compute_py, compute_noise_matrix_from_inverse


# In[ ]:


def num_label_errors(
    labels, 
    psx, 
    confident_joint = None,
):
    '''Estimates the number of label errors in `labels`.

    Parameters
    ----------

    labels : np.array
        A discrete vector of noisy labels, i.e. some labels may be erroneous.

    psx : np.array (shape (N, K))
        P(label=k|x) is a matrix with K (noisy) probabilities for each of the N examples x.
        This is the probability distribution over all K classes, for each
        example, regarding whether the example has label s==k P(s=k|x). psx should
        have been computed using 3 (or higher) fold cross-validation.
        
    confident_joint : np.array (shape (K, K), type int)
        A K,K integer matrix of count(s=k, y=k). Estimates a confident subset of
        the joint disribution of the noisy and true labels P_{s,y}.
        Each entry in the matrix contains the number of examples confidently 
        counted into every pair (s=j, y=k) classes.
    
    Returns
    -------
        An integer estimating the number of label errors.'''
    
    if confident_joint is None:
        confident_joint = estimate_confident_joint_from_probabilities(
            s = labels, 
            psx = psx,
        )
        
    # Normalize confident joint so that it estimates the joint, p(s,y)
    joint = confident_joint / float(np.sum(confident_joint))
    frac_errors = 1. - joint.trace()
    num_errors = int(frac_errors * len(labels))
        
    return num_errors


def calibrate_confident_joint(confident_joint, s, psx):
    '''Calibrates any confident joint estimate P(s=i, y=j) such that
    np.sum(cj) == len(s) and np.sum(cj, axis = 1) == np.bincount(s).
    
    In other words, this function forces the confident joint to have the
    true noisy prior p(s) (summed over columns for each row) and also
    forces the confident joint to add up to the total number of examples.
    
    This method makes the confident joint a valid counts estimate
    of the actual joint of noisy and true labels.
    
    Parameters
    ----------
        
    confident_joint : np.array (shape (K, K))
        A K,K integer matrix of count(s=k, y=k). Estimates a confident subset of
        the joint disribution of the noisy and true labels P_{s,y}.
        Each entry in the matrix contains the number of examples confidently 
        counted into every pair (s=j, y=k) classes.

    s : np.array
        A discrete vector of labels, s, which may contain mislabeling. "s" denotes
        the noisy label instead of \tilde(y), for ASCII encoding reasons.

    psx : np.array (shape (N, K))
        P(s=k|x) is a matrix with K (noisy) probabilities for each of the N examples x.
        This is the probability distribution over all K classes, for each
        example, regarding whether the example has label s==k P(s=k|x). psx should
        have been computed using 3 (or higher) fold cross-validation.
    
    Returns
    -------
        An np.array of shape (K, K) of type float representing a valid
        estimate of the joint COUNTS of noisy and true labels.
    '''
    
    s_counts = np.bincount(s)
    confident_joint = compute_confident_joint(s, psx)
    # Calibrate confident joint to have correct p(s) prior on noisy labels.
    calibrated_cj = (confident_joint.T / confident_joint.sum(axis=1) * s_counts).T
    # Calibrate confident joint to sum to 1 (now an estimate of true joint counts)
    calibrated_cj = calibrated_cj / np.sum(calibrated_cj) * len(s)
    
    # Check calibration
    assert(all(calibrated_cj.sum(axis = 1).round().astype(int) == s_counts))
    assert(len(s) == int(round(np.sum(calibrated_cj))))
    
    return calibrated_cj


def estimate_joint(confident_joint, s, psx):
    '''Estimates the joint distribution of label noise P(s=i, y=j) guranteed to
      * sum to 1
      * np.sum(joint_estimate, axis = 1) == p(s)
    
    Parameters
    ----------
    See cleanlab.latent_estimation.calibrate_confident_joint docstring.
    
    Returns
    -------
        An np.array of shape (K, K) of type float representing a valid
        estimate of the true joint of noisy and true labels.
    '''
    
    calibrated_cj = calibrate_confident_joint(confident_joint, s, psx)
    return calibrated_cj / float(sum(calibrated_cj))


def compute_confident_joint(
    s, 
    psx, 
    K = None,
    thresholds = None, 
):
    '''Estimates P(s,y), the confident counts of the latent 
    joint distribution of true and noisy labels 
    using observed s and predicted probabilities psx.
    
    This estimate is called the confident joint. 

    Parameters
    ----------

    s : np.array
        A discrete vector of labels, s, which may contain mislabeling. "s" denotes
        the noisy label instead of \tilde(y), for ASCII encoding reasons.

    psx : np.array (shape (N, K))
        P(s=k|x) is a matrix with K (noisy) probabilities for each of the N examples x.
        This is the probability distribution over all K classes, for each
        example, regarding whether the example has label s==k P(s=k|x). psx should
        have been computed using 3 (or higher) fold cross-validation.
        
    K : int (default: None)
        Number of unique classes. Calculated as len(np.unique(s)) when K == None.

    thresholds : iterable (list or np.array) of shape (K, 1)  or (K,)
        P(s^=k|s=k). If an example has a predicted probability "greater" than 
        this threshold, it is counted as having hidden label y = k. This is 
        not used for pruning, only for estimating the noise rates using 
        confident counts. This value should be between 0 and 1. Default is None.'''
    
    # s needs to be a numpy array
    s = np.asarray(s)
    
    # Find the number of unique classes if K is not given
    if K is None:
        K = len(np.unique(s))
    
    # Estimate the probability thresholds for confident counting 
    if thresholds is None:
        thresholds = [np.mean(psx[:,k][s == k]) for k in range(K)] # P(s^=k|s=k)
    thresholds = np.asarray(thresholds)

    # The following code computes the confident joint.
    # The code is optimized with vectorized functions.
    # For ease of understanding, here is (a slow) implementation with for loops.
    #     confident_joint = np.zeros((K, K), dtype = int)
    #     for i, row in enumerate(psx):
    #         s_label = s[i]
    #         confident_bins = row >= thresholds - 1e-6
    #         num_confident_bins = sum(confident_bins)
    #         if num_confident_bins == 1:
    #             confident_joint[s_label][np.argmax(confident_bins)] += 1
    #         elif num_confident_bins > 1:
    #             confident_joint[s_label][np.argmax(row)] += 1
    
    # Compute confident joint (vectorized for speed).
    
    # psx_bool is a bool matrix where each row represents a training example as
    # a boolean vector of size K, with True if the example confidentally belongs
    # to that class and False if not.
    psx_bool = (psx >= thresholds - 1e-6) 
    num_confident_bins = psx_bool.sum(axis = 1)
    at_least_one_confident = num_confident_bins > 0
    more_than_one_confident = num_confident_bins > 1
    psx_argmax = psx.argmax(axis = 1)
    # Note that confident_argmax is meaningless for rows of all False
    confident_argmax = psx_bool.argmax(axis = 1)
    # For each example, choose the confident class (greater than threshold)
    # When there is more than one confident class, choose the class with largest prob.
    true_label_guess = np.where(more_than_one_confident, psx_argmax, confident_argmax)
    y_confident = true_label_guess[at_least_one_confident] # Omits meaningless all-False rows
    s_confident = s[at_least_one_confident]
    from sklearn.metrics import confusion_matrix
    confident_joint = confusion_matrix(y_confident, s_confident).T
    
    return confident_joint


def estimate_confident_joint_from_probabilities(
    s, 
    psx, 
    thresholds = None, 
    force_ps = False,
    return_list_of_converging_cj_matrices = False,
):
    '''Estimates P(s,y), the confident counts of the latent 
    joint distribution of true and noisy labels 
    using observed s and predicted probabilities psx.

    Important! This function assumes that psx are out-of-sample 
    holdout probabilities. This can be done with cross validation. If
    the probabilities are not computed out-of-sample, overfitting may occur.

    This function estimates the joint of shape (K, K). This is the
    confident counts of examples in every class, labeled as every other class.

    Under certain conditions, estimates are exact, and in most
    conditions, the estimate is within 1 percent of the truth.
    
    We provide a for-loop based implementation of the confident joint
    below. This implementation is not efficient and not used in practice,
    but it makes clear how the confident joint is computed:

    # Confident examples are those that we are confident have label y = k
    # Estimate the (K, K) matrix of confident examples with s = k_s and y = k_y
    cj = np.zeros((K, K))
    for k_s in range(K): # k_s is the class value k of noisy label s
        for k_y in range(K): # k_y is the (guessed) class value k of true label y
            cj[k_s][k_y] = sum((psx[:,k_y] >= (thresholds[k_y] - 1e-8)) & (s == k_s))

    Parameters
    ----------

    s : np.array
        A discrete vector of labels, s, which may contain mislabeling. "s" denotes
        the noisy label instead of \tilde(y), for ASCII encoding reasons.

    psx : np.array (shape (N, K))
        P(s=k|x) is a matrix with K (noisy) probabilities for each of the N examples x.
        This is the probability distribution over all K classes, for each
        example, regarding whether the example has label s==k P(s=k|x). psx should
        have been computed using 3 (or higher) fold cross-validation.

    thresholds : iterable (list or np.array) of shape (K, 1)  or (K,)
        P(s^=k|s=k). If an example has a predicted probability "greater" than 
        this threshold, it is counted as having hidden label y = k. This is 
        not used for pruning, only for estimating the noise rates using 
        confident counts. This value should be between 0 and 1. Default is None.
        
    force_ps : bool or int
        If true, forces the output confident_joint matrix to have p(s) closer to the true
        p(s). The method used is SGD with a learning rate of eta = 0.5.
        If force_ps is an integer, it represents the number of epochs.
        Setting this to True is not always good. To make p(s) match, fewer confident
        examples are used to estimate the confident_joint, resulting in poorer estimation of
        the overall matrix even if p(s) is more accurate. 
        
    return_list_of_converging_cj_matrices : bool (default = False)
        When force_ps is true, it converges the joint count matrix that is returned.
        Setting this to true will return the list of the converged matrices. The first
        item in the list is the original and the last item is the final result.

    Output
    ------
        confident_joint matrix count(s, y) : np.array (shape (K, K))'''
    
    # Number of classes
    K = len(np.unique(s))  
    # 'ps' is p(s=k)
    ps = value_counts(s) / float(len(s))        
    # Estimate the probability thresholds for confident counting 
    s = np.asarray(s)
    if thresholds is None:
        thresholds = [np.mean(psx[:,k][s == k]) for k in range(K)] # P(s^=k|s=k)
    thresholds = np.asarray(thresholds)         
    # joint counts
    cjs = []   
    sgd_epochs = 5 if force_ps is True else 1 # Default 5 epochs if force_ps
    if type(force_ps) == int:
        sgd_epochs = force_ps
    for sgd_iteration in range(sgd_epochs):          
        # Compute the confident joint. 
        confident_joint = compute_confident_joint(s, psx, K, thresholds)
        confident_joint = calibrate_confident_joint(confident_joint, s, psx)
        cjs.append(confident_joint)
        
        if force_ps:
            joint_ps = confident_joint.sum(axis=1) / float(np.sum(confident_joint))
            # Update thresholds (SGD) to converge p(s) of joint with actual p(s)    
            eta = 0.5 # learning rate
            thresholds += eta * (joint_ps - ps)
        else: # Do not converge p(s) of joint with actual p(s)
            break
            
    return cjs if return_list_of_converging_cj_matrices else confident_joint
 
    
def estimate_latent(
    confident_joint, 
    s, 
    py_method = 'cnt', 
    converge_latent_estimates = False,
):
    '''Computes the latent prior p(y), the noise matrix P(s|y) and the
    inverse noise matrix P(y|s) from the confident_joint count(s, y). The
    confident_joint estimated by estimate_confident_joint_from_probabilities()
    by counting confident examples.

    Parameters
    ----------

    s : np.array
        A discrete vector of labels, s, which may contain mislabeling. "s" denotes
        the noisy label instead of \tilde(y), for ASCII encoding reasons.
        
    confident_joint : np.array (shape (K, K), type int)
        A K,K integer matrix of count(s=k, y=k). Estimatesa a confident subset of
        the joint disribution of the noisy and true labels P_{s,y}.
        Each entry in the matrix contains the number of examples confidently 
        counted into every pair (s=j, y=k) classes.
        
    py_method : str (Options: ["cnt", "eqn", "marginal", "marginal_ps"])
        How to compute the latent prior p(y=k). Default is "cnt" as it often
        works well even when the noise matrices are estimated poorly by using
        the matrix diagonals instead of all the probabilities.

    converge_latent_estimates : bool
      If true, forces numerical consistency of estimates. Each is estimated
      independently, but they are related mathematically with closed form 
      equivalences. This will iteratively make them mathematically consistent.

    Output
    ------
        A tuple containing (py, noise_matrix, inv_noise_matrix).'''
    
    # Number of classes
    K = len(np.unique(s))  
    # 'ps' is p(s=k)
    ps = value_counts(s) / float(len(s))
    
    # Ensure labels are of type np.array()
    s = np.asarray(s)
    
    # Number of training examples confidently counted from each noisy class
    s_count = confident_joint.sum(axis=1).astype(float)
    
    # Number of training examples confidently counted into each true class
    y_count = confident_joint.sum(axis=0).astype(float)
    
    # Confident Counts Estimator for p(s=k_s|y=k_y) ~ |s=k_s and y=k_y| / |y=k_y|
    noise_matrix = confident_joint / y_count

    # Confident Counts Estimator for p(y=k_y|s=k_s) ~ |y=k_y and s=k_s| / |s=k_s|
    inv_noise_matrix = confident_joint.T / s_count
    
    # Compute the prior p(y), the latent (uncorrupted) class distribution.
    py = compute_py(ps, noise_matrix, inv_noise_matrix, py_method, y_count)
    
    noise_matrix = clip_noise_rates(noise_matrix) 
    inv_noise_matrix = clip_noise_rates(inv_noise_matrix)

    if converge_latent_estimates:
        py, noise_matrix, inv_noise_matrix = converge_estimates(ps, py, noise_matrix, inv_noise_matrix)
        # Again clip py and noise rates into proper range [0,1)
        py = clip_values(py, low=1e-5, high=1.0, new_sum = 1.0) 
        noise_matrix = clip_noise_rates(noise_matrix) 
        inv_noise_matrix = clip_noise_rates(inv_noise_matrix)

    return py, noise_matrix, inv_noise_matrix                  
    
    
def estimate_py_and_noise_matrices_from_probabilities(
    s, 
    psx, 
    thresholds = None,
    converge_latent_estimates = True,
    force_ps = False,
    py_method = 'cnt', 
):
    '''Computes the confident counts
    estimate of latent variables py and the noise rates 
    using observed s and predicted probabilities psx.

    Important! This function assumes that psx are out-of-sample 
    holdout probabilities. This can be done with cross validation. If
    the probabilities are not computed out-of-sample, overfitting may occur.

    This function estimates the noise_matrix of shape (K, K). This is the
    fraction of examples in every class, labeled as every other class. The
    noise_matrix is a conditional probability matrix for P(s=k_s|y=k_y).

    Under certain conditions, estimates are exact, and in most
    conditions, estimates are within one percent of the actual noise rates.

    Parameters
    ----------

    s : np.array
      A discrete vector of labels, s, which may contain mislabeling. "s" denotes
      the noisy label instead of \tilde(y), for ASCII encoding reasons.

    psx : np.array (shape (N, K))
      P(s=k|x) is a matrix with K (noisy) probabilities for each of the N examples x.
      This is the probability distribution over all K classes, for each
      example, regarding whether the example has label s==k P(s=k|x). psx should
      have been computed using 3 (or higher) fold cross-validation.

    thresholds : iterable (list or np.array) of shape (K, 1)  or (K,)
      P(s^=k|s=k). If an example has a predicted probability "greater" than 
      this threshold, it is counted as having hidden label y = k. This is 
      not used for pruning, only for estimating the noise rates using 
      confident counts. This value should be between 0 and 1. Default is None.

    converge_latent_estimates : bool
      If true, forces numerical consistency of estimates. Each is estimated
      independently, but they are related mathematically with closed form 
      equivalences. This will iteratively make them mathematically consistent. 
        
    force_ps : bool or int
        If true, forces the output confident_joint matrix to have p(s) closer to the true
        p(s). The method used is SGD with a learning rate of eta = 0.5.
        If force_ps is an integer, it represents the number of epochs.
        Setting this to True is not always good. To make p(s) match, fewer confident
        examples are used to estimate the confident_joint, resulting in poorer estimation of
        the overall matrix even if p(s) is more accurate. 
        
    py_method : str
        How to compute the latent prior p(y=k). Default is "cnt" as it tends to
        work best, but you may also set this hyperparameter to "eqn" or "marginal".

    Output
    ------
        py, noise_matrix, inverse_noise_matrix'''
  
    confident_joint = estimate_confident_joint_from_probabilities(s, psx, thresholds, force_ps)
    py, noise_matrix, inv_noise_matrix = estimate_latent(        
        confident_joint=confident_joint, 
        s=s, 
        py_method=py_method, 
        converge_latent_estimates=converge_latent_estimates,
    )
    
    return py, noise_matrix, inv_noise_matrix, confident_joint


def estimate_confident_joint_and_cv_pred_proba(
    X, 
    s, 
    clf = logreg(multi_class = 'auto', solver = 'lbfgs'),
    cv_n_folds = 5,
    thresholds = None,
    force_ps = False,
    return_list_of_converging_cj_matrices = False,
    seed = None,
):
    '''Estimates P(s,y), the confident counts of the latent 
    joint distribution of true and noisy labels 
    using observed s and predicted probabilities psx. 

    The output of this function is a numpy array of shape (K, K). 

    Under certain conditions, estimates are exact, and in many
    conditions, estimates are within one percent of actual.
    
    Notes: There are two ways to compute the confident joint with pros/cons.
    1. For each holdout set, we compute the confident joint, then sum them up.
    2. We get all the pred_proba, combine them, compute the confident joint on all.
    (1) is more accurate because it computes the appropriate thresholds for each fold
    (2) is more accurate when you have only a little data because it computes 
    the confident joint using all the probabilities. For example if you had only 100
    examples, with 5-fold cross validation and uniform p(y) you would only have 20
    examples to compute each confident joint for (1). Such small amounts of data
    is bound to result in estimation errors. For this reason, we implement (2),
    but we implement (1) as a commented out function at the end of this file.

    Parameters
    ----------
    X : np.array
      Input feature matrix (N, D), 2D numpy array

    s : np.array
      A discrete vector of labels, s, which may contain mislabeling. "s" denotes
      the noisy label instead of \tilde(y), for ASCII encoding reasons.

    clf : sklearn.classifier or equivalent
      Default classifier used is logistic regression. Assumes clf
      has predict_proba() and fit() defined.

    cv_n_folds : int
      The number of cross-validation folds used to compute
      out-of-sample probabilities for each example in X.

    thresholds : iterable (list or np.array) of shape (K, 1)  or (K,)
      P(s^=k|s=k). If an example has a predicted probability "greater" than 
      this threshold, it is counted as having hidden label y = k. This is 
      not used for pruning, only for estimating the noise rates using 
      confident counts. This value should be between 0 and 1. Default is None.
        
    force_ps : bool or int
        If true, forces the output confident_joint matrix to have p(s) closer to the true
        p(s). The method used is SGD with a learning rate of eta = 0.5.
        If force_ps is an integer, it represents the number of epochs.
        Setting this to True is not always good. To make p(s) match, fewer confident
        examples are used to estimate the confident_joint, resulting in poorer estimation of
        the overall matrix even if p(s) is more accurate. 
        
    return_list_of_converging_cj_matrices : bool (default = False)
        When force_ps is true, it converges the joint count matrix that is returned.
        Setting this to true will return the list of the converged matrices. The first
        item in the list is the original and the last item is the final result.
        
    seed : int (default = None)
        Number to set the default state of the random number generator used to split 
        the cross-validated folds. If None, uses np.random current random state.

    Output
    ------
      Returns a tuple of two numpy array matrices in the form:
      (joint counts matrix, predicted probability matrix)'''
  
    # Number of classes
    K = len(np.unique(s))  
    # 'ps' is p(s=k)
    ps = value_counts(s) / float(len(s))
    
    # Ensure labels are of type np.array()
    s = np.asarray(s)

    # Create cross-validation object for out-of-sample predicted probabilities.
    # CV folds preserve the fraction of noisy positive and
    # noisy negative examples in each class.
    kf = StratifiedKFold(n_splits = cv_n_folds, shuffle = True, random_state = seed)

    # Intialize psx array
    psx = np.zeros((len(s), K))

    # Split X and s into "cv_n_folds" stratified folds.
    for k, (cv_train_idx, cv_holdout_idx) in enumerate(kf.split(X, s)):
        
        clf_copy = copy.deepcopy(clf)

        # Select the training and holdout cross-validated sets.
        X_train_cv, X_holdout_cv = X[cv_train_idx], X[cv_holdout_idx]
        s_train_cv, s_holdout_cv = s[cv_train_idx], s[cv_holdout_idx]

        # Fit the clf classifier to the training set and 
        # predict on the holdout set and update psx. 
        clf_copy.fit(X_train_cv, s_train_cv)
        psx_cv = clf_copy.predict_proba(X_holdout_cv) # P(s = k|x) # [:,1]
        psx[cv_holdout_idx] = psx_cv

    # Compute the confident counts of all pairwise label-flipping mislabeling rates.
    confident_joint = estimate_confident_joint_from_probabilities(
        s = s, 
        psx = psx, # P(s = k|x)
        thresholds = thresholds, 
        force_ps = force_ps,
        return_list_of_converging_cj_matrices = return_list_of_converging_cj_matrices,
    )
    
    return confident_joint, psx


def estimate_py_noise_matrices_and_cv_pred_proba(
    X, 
    s, 
    clf = logreg(multi_class = 'auto', solver = 'lbfgs'),
    cv_n_folds = 5,
    thresholds = None,
    converge_latent_estimates = False,
    force_ps = False,
    return_list_of_converging_cj_matrices = False,
    py_method = 'cnt',
    seed = None,
):
    '''This function computes the out-of-sample predicted 
    probability P(s=k|x) for every example x in X using cross
    validation while also computing the confident counts noise
    rates within each cross-validated subset and returning
    the average noise rate across all examples. 

    This function estimates the noise_matrix of shape (K, K). This is the
    fraction of examples in every class, labeled as every other class. The
    noise_matrix is a conditional probability matrix for P(s=k_s|y=k_y).

    Under certain conditions, estimates are exact, and in most
    conditions, estimates are within one percent of the actual noise rates.

    Parameters
    ----------
    X : np.array
      Input feature matrix (N, D), 2D numpy array

    s : np.array
      A discrete vector of labels, s, which may contain mislabeling. "s" denotes
      the noisy label instead of \tilde(y), for ASCII encoding reasons.

    clf : sklearn.classifier or equivalent
      Default classifier used is logistic regression. Assumes clf
      has predict_proba() and fit() defined.

    cv_n_folds : int
      The number of cross-validation folds used to compute
      out-of-sample probabilities for each example in X.

    thresholds : iterable (list or np.array) of shape (K, 1)  or (K,)
      P(s^=k|s=k). If an example has a predicted probability "greater" than 
      this threshold, it is counted as having hidden label y = k. This is 
      not used for pruning, only for estimating the noise rates using 
      confident counts. This value should be between 0 and 1. Default is None.

    converge_latent_estimates : bool
      If true, forces numerical consistency of estimates. Each is estimated
      independently, but they are related mathematically with closed form 
      equivalences. This will iteratively make them mathematically consistent.
        
    force_ps : bool or int
        If true, forces the output confident_joint matrix to have p(s) closer to the true
        p(s). The method used is SGD with a learning rate of eta = 0.5.
        If force_ps is an integer, it represents the number of epochs.
        Setting this to True is not always good. To make p(s) match, fewer confident
        examples are used to estimate the confident_joint, resulting in poorer estimation of
        the overall matrix even if p(s) is more accurate. 
        
    return_list_of_converging_cj_matrices : bool (default = False)
        When force_ps is true, it converges the joint count matrix that is returned.
        Setting this to true will return the list of the converged matrices. The first
        item in the list is the original and the last item is the final result.
        
    py_method : str
        How to compute the latent prior p(y=k). Default is "cnt" as it tends to
        work best, but you may also set this hyperparameter to "eqn" or "marginal".
        
    seed : int (default = None)
        Number to set the default state of the random number generator used to split 
        the cross-validated folds. If None, uses np.random current random state.

    Output
    ------
      Returns a tuple of five numpy array matrices in the form:
      (py, noise_matrix, inverse_noise_matrix, 
      joint count matrix i.e. confident joint, predicted probability matrix)'''
    
    confident_joint, psx = estimate_confident_joint_and_cv_pred_proba(
        X = X, 
        s = s, 
        clf = clf,
        cv_n_folds = cv_n_folds,
        thresholds = thresholds,
        force_ps = force_ps,
        return_list_of_converging_cj_matrices = return_list_of_converging_cj_matrices,
        seed = seed,
    )
    
    py, noise_matrix, inv_noise_matrix = estimate_latent(
        confident_joint = confident_joint, 
        s = s, 
        py_method = py_method, 
        converge_latent_estimates = converge_latent_estimates,
    )
    
    return py, noise_matrix, inv_noise_matrix, confident_joint, psx 


def estimate_cv_predicted_probabilities(
    X, 
    labels, # class labels can be noisy (s) or not noisy (y).
    clf = logreg(multi_class = 'auto', solver = 'lbfgs'),
    cv_n_folds = 5,
    seed = None,
):
    '''This function computes the out-of-sample predicted 
    probability [P(s=k|x)] for every example in X using cross
    validation. Output is a np.array of shape (N, K) where N is 
    the number of training examples and K is the number of classes.

    Parameters
    ----------
    
    X : np.array
      Input feature matrix (N, D), 2D numpy array

    labels : np.array or list of ints from [0,1,..,K-1]
      A discrete vector of class labels which may or may not contain mislabeling

    clf : sklearn.classifier or equivalent
      Default classifier used is logistic regression. Assumes clf
      has predict_proba() and fit() defined.

    cv_n_folds : int
      The number of cross-validation folds used to compute
      out-of-sample probabilities for each example in X.
        
    seed : int (default = None)
        Number to set the default state of the random number generator used to split 
        the cross-validated folds. If None, uses np.random current random state.
    '''

    return estimate_py_noise_matrices_and_cv_pred_proba(
        X = X, 
        s = labels, 
        clf = clf,
        cv_n_folds = cv_n_folds,
        seed = seed,
    )[-1]


def estimate_noise_matrices(
    X, 
    s, 
    clf = logreg(multi_class = 'auto', solver = 'lbfgs'),
    cv_n_folds = 5,
    thresholds = None,
    converge_latent_estimates = True,
    seed = None,
):
    '''Estimates the noise_matrix of shape (K, K). This is the
    fraction of examples in every class, labeled as every other class. The
    noise_matrix is a conditional probability matrix for P(s=k_s|y=k_y).

    Under certain conditions, estimates are exact, and in most
    conditions, estimates are within one percent of the actual noise rates.

    Parameters
    ----------
    X : np.array
      Input feature matrix (N, D), 2D numpy array

    s : np.array
      A discrete vector of labels, s, which may contain mislabeling

    clf : sklearn.classifier or equivalent
      Default classifier used is logistic regression. Assumes clf
      has predict_proba() and fit() defined.

    cv_n_folds : int
      The number of cross-validation folds used to compute
      out-of-sample probabilities for each example in X.

    thresholds : iterable (list or np.array) of shape (K, 1)  or (K,)
      P(s^=k|s=k). If an example has a predicted probability "greater" than 
      this threshold, it is counted as having hidden label y = k. This is 
      not used for pruning, only for estimating the noise rates using 
      confident counts. This value should be between 0 and 1. Default is None.

    converge_latent_estimates : bool
      If true, forces numerical consistency of estimates. Each is estimated
      independently, but they are related mathematically with closed form 
      equivalences. This will iteratively make them mathematically consistent.
        
    seed : int (default = None)
        Number to set the default state of the random number generator used to split 
        the cross-validated folds. If None, uses np.random current random state.

    Output
    ------
        A two-item tuple containing (noise_matrix, inv_noise_matrix).'''

    return estimate_py_noise_matrices_and_cv_pred_proba(
        X = X, 
        s = s, 
        clf = clf,
        cv_n_folds = cv_n_folds,
        thresholds = thresholds,
        converge_latent_estimates = converge_latent_estimates,
        seed = seed,
    )[1:-2]


def converge_estimates(
    ps,
    py,
    noise_matrix, 
    inverse_noise_matrix, 
    inv_noise_matrix_iterations = 5,
    noise_matrix_iterations = 3,
):
    '''Computes py := P(y=k) and both noise_matrix and inverse_noise_matrix,
    by numerically converging ps := P(s=k), py, and the noise matrices.

    Forces numerical consistency of estimates. Each is estimated
    independently, but they are related mathematically with closed form 
    equivalences. This will iteratively make them mathematically consistent. 

    py := P(y=k) and the inverse noise matrix P(y=k_y|s=k_s) specify one another, 
    meaning one can be computed from the other and vice versa. When numerical
    discrepancy exists due to poor estimation, they can be made to agree by repeatedly
    computing one from the other, for some a certain number of iterations (3-10 works fine.)

    Do not set iterations too high or performance will decrease as small deviations
    will get perturbated over and over and potentially magnified.

    Note that we have to first converge the inverse_noise_matrix and py, 
    then we can update the noise_matrix, then repeat. This is becauase the
    inverse noise matrix depends on py (which is unknown/latent), but the
    noise matrix depends on ps (which is known), so there will be no change
    in the noise matrix if we recompute it when py and inverse_noise_matrix change.


    Parameters
    ----------

    ps : np.array (shape (K, ) or (1, K))
        The fraction (prior probability) of each observed, noisy class label, P(y = k).

    noise_matrix : np.array of shape (K, K), K = number of classes 
        A conditional probablity matrix of the form P(s=k_s|y=k_y) containing
        the fraction of examples in every class, labeled as every other class.
        Assumes columns of noise_matrix sum to 1.

    inverse_noise_matrix : np.array of shape (K, K), K = number of classes 
        A conditional probablity matrix of the form P(y=k_y|s=k_s) representing
        the estimated fraction observed examples in each class k_s, that are
        mislabeled examples from every other class k_y. If None, the 
        inverse_noise_matrix will be computed from psx and s.
        Assumes columns of inverse_noise_matrix sum to 1.

    Output
    ------  
        Three np.arrays of the form (py, noise_matrix, inverse_noise_matrix) with py 
        and inverse_noise_matrix and noise_matrix having numerical agreement.'''  
  
    for j in range(noise_matrix_iterations):
        for i in range(inv_noise_matrix_iterations):
            inverse_noise_matrix = compute_inv_noise_matrix(py, noise_matrix, ps)
            py = compute_py(ps, noise_matrix, inverse_noise_matrix)
        noise_matrix = compute_noise_matrix_from_inverse(ps, inverse_noise_matrix, py)
    
    return py, noise_matrix, inverse_noise_matrix

